"""Build a hybrid (BM25 + vector) index over the PVG knowledge release.

Reads knowledge/bundles/<release>/*.md, filters by status/validity window,
and writes:
  knowledge/index/docs.jsonl       - doc metadata + text
  knowledge/index/embeddings.npy   - Jina zh embeddings (L2-normalized)

Run: uv run --extra rag python scripts/build_knowledge_index.py
"""

import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import jieba
import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

# 路径支持环境变量覆盖（RAG 服务 /reindex 经 PVG_BUNDLES_DIR/PVG_KNOWLEDGE_INDEX 注入）
BUNDLE_DIR = Path(os.environ.get("PVG_BUNDLES_DIR", "knowledge/bundles/v2-miniprogram-20260719b"))
INDEX_DIR = Path(os.environ.get("PVG_KNOWLEDGE_INDEX", "knowledge/index"))
MODEL_NAME = "jinaai/jina-embeddings-v2-base-zh"
BATCH_SIZE = 16

CST = timezone(timedelta(hours=8))


def parse_doc(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    # Split into YAML-ish header blocks and markdown body on '---' fences
    parts = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    # parts: ['', header1, header2?, body...]
    body = parts[-1].strip() if len(parts) >= 2 else text
    header = parts[1] if len(parts) >= 3 else ""

    def field(name: str) -> str:
        m = re.search(rf"^{name}:\s*['\"]?(.*?)['\"]?\s*$", header, flags=re.MULTILINE)
        return m.group(1) if m else ""

    status = field("status")
    if status and status != "published":
        return None

    def parse_dt(v: str) -> datetime | None:
        try:
            return datetime.fromisoformat(v).astimezone(CST)
        except ValueError:
            return None

    now = datetime.now(CST)
    vf, vu = parse_dt(field("valid_from")), parse_dt(field("valid_until"))
    if vf and now < vf:
        return None
    if vu and now > vu:
        return None

    title = field("title")
    return {
        "id": path.stem,
        "title": title,
        "knowledge_type": field("knowledge_type"),
        "valid_until": field("valid_until"),
        "text": f"{title}\n{body}",
    }


def main() -> None:
    docs = []
    for p in sorted(BUNDLE_DIR.glob("*.md")):
        d = parse_doc(p)
        if d:
            docs.append(d)
    print(f"parsed {len(docs)} valid docs")

    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
    model = AutoModel.from_pretrained(MODEL_NAME, trust_remote_code=True)
    model.eval()

    embeddings = []
    with torch.no_grad():
        for i in range(0, len(docs), BATCH_SIZE):
            batch = [d["text"][:1000] for d in docs[i : i + BATCH_SIZE]]
            enc = tokenizer(batch, padding=True, truncation=True, max_length=512, return_tensors="pt")
            out = model(**enc).last_hidden_state
            mask = enc["attention_mask"].unsqueeze(-1).float()
            emb = (out * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            emb = torch.nn.functional.normalize(emb, p=2, dim=1)
            embeddings.append(emb.numpy())
            print(f"embedded {min(i + BATCH_SIZE, len(docs))}/{len(docs)}")

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    np.save(INDEX_DIR / "embeddings.npy", np.vstack(embeddings))
    with open(INDEX_DIR / "docs.jsonl", "w", encoding="utf-8") as f:
        for d in docs:
            d["tokens"] = list(jieba.cut_for_search(d["text"]))
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"index written to {INDEX_DIR}")


if __name__ == "__main__":
    main()
