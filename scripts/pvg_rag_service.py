"""PVG 知识检索微服务（Hybrid RAG：BM25 + 向量 + RRF）。

从 pvg_mcp_proxy.py 抽出的独立检索服务（对应 PRD 3.2「RAG 做成独立微服务」），
为管理后台提供分阶段召回观测接口（FR-804 召回测试台）与知识库管理数据源。

接口：
  GET  /health   — 存活与索引概况
  POST /search   — 生产检索（BM25 top50 + 向量 top50 → RRF k=60 → top N）
  POST /stages   — 分阶段召回（FR-804）：BM25/向量/RRF 各阶段结果与得分
  POST /reload   — 热加载索引（重建索引后无需重启进程）
  POST /reindex  — 重建索引（子进程跑 build_knowledge_index.py）并自动热加载，防并发
  GET  /docs     — 文档浏览（分页/过滤，管理后台知识库管理数据源）
  GET  /docs/{doc_id} — 单篇完整字段（text 全文）
  GET  /gaps     — 知识缺口看板（检索为空/低分 query 聚合 Top N）
  GET  /gaps/stats — 缺口统计（窗口内无命中率）

查询日志：/search 与 /stages 每次调用旁路记录（内存环形缓冲 2000 条；
环境变量 PVG_QUERY_LOG 给 jsonl 路径则同时落盘），不影响既有检索行为。

运行：
  uv run --extra rag python scripts/pvg_rag_service.py     # 本地：127.0.0.1:8901
环境变量：
  PVG_KNOWLEDGE_INDEX（默认 knowledge/index）、PVG_RAG_PORT（默认 8901）、
  PVG_RAG_HOST（默认 127.0.0.1，容器内为 0.0.0.0）、PVG_QUERY_LOG（可选，查询日志落盘路径）、
  PVG_BUNDLES_DIR（可选，/reindex 时传给索引构建子进程的 bundle 目录）
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

KNOWLEDGE_INDEX_DIR = Path(os.environ.get("PVG_KNOWLEDGE_INDEX", "knowledge/index"))
QUERY_LOG_PATH = os.environ.get("PVG_QUERY_LOG") or None  # 给 jsonl 路径则查询日志同时落盘
BUNDLES_DIR = os.environ.get("PVG_BUNDLES_DIR") or None  # /reindex 时传给索引构建子进程
BUILD_SCRIPT = Path(__file__).resolve().parent / "build_knowledge_index.py"
REINDEX_TIMEOUT_S = 900  # 重建索引（含向量计算）上限，正常 1-3 分钟
EMBEDDING_MODEL_NAME = "jinaai/jina-embeddings-v2-base-zh"
RRF_K = 60
BM25_TOP = 50
VEC_TOP = 50

CST = timezone(timedelta(hours=8), "Asia/Shanghai")  # 有效期判定统一东八区
EXPIRING_DAYS = 7  # 7 天内到期算 expiring
HIT_THRESHOLD = 0.01  # rrf 首名分达到该值才算命中
QUERY_LOG_MAX = 2000  # 查询日志内存环形缓冲上限

_rag: dict[str, Any] = {
    "docs": None, "bm25": None, "embeddings": None,
    "model": None, "tokenizer": None, "mtime": None, "index_mtime": None,
}
_lock = threading.Lock()

# 查询日志：内存环形缓冲（进程重启即清空；需长期留存请配 PVG_QUERY_LOG 落盘）
_query_log: deque[dict[str, Any]] = deque(maxlen=QUERY_LOG_MAX)


def _load_docs() -> None:
    """轻量加载 docs.jsonl（/docs /gaps 用，不触发 BM25/向量/模型等重型构建）。

    以 docs.jsonl 的 mtime 判断是否刷新；/reload 置空 mtime 后随下次访问重建。
    """
    mtime = (KNOWLEDGE_INDEX_DIR / "docs.jsonl").stat().st_mtime
    if _rag["docs"] is not None and _rag["mtime"] == mtime:
        return
    with _lock:
        if _rag["docs"] is not None and _rag["mtime"] == mtime:
            return
        docs = []
        with open(KNOWLEDGE_INDEX_DIR / "docs.jsonl", encoding="utf-8") as f:
            for line in f:
                docs.append(json.loads(line))
        _rag["docs"] = docs
        _rag["mtime"] = mtime


def _load_rag() -> None:
    """加载（或热加载）检索索引；docs 变更（mtime 变化）时才重建重型内存结构。"""
    _load_docs()
    if _rag["bm25"] is not None and _rag["index_mtime"] == _rag["mtime"]:
        return
    with _lock:
        if _rag["bm25"] is not None and _rag["index_mtime"] == _rag["mtime"]:
            return
        import jieba  # noqa: F401
        import numpy as np
        import torch
        from rank_bm25 import BM25Okapi
        from transformers import AutoModel, AutoTokenizer

        _rag["bm25"] = BM25Okapi([d["tokens"] for d in _rag["docs"]])
        _rag["embeddings"] = np.load(KNOWLEDGE_INDEX_DIR / "embeddings.npy")
        if _rag["model"] is None:
            _rag["tokenizer"] = AutoTokenizer.from_pretrained(
                EMBEDDING_MODEL_NAME, trust_remote_code=True
            )
            model = AutoModel.from_pretrained(EMBEDDING_MODEL_NAME, trust_remote_code=True)
            model.eval()
            _rag["model"] = model
            _rag["torch"] = torch
            _rag["jieba"] = jieba
            _rag["np"] = np
        _rag["index_mtime"] = _rag["mtime"]


def _embed_query(query: str):
    torch = _rag["torch"]
    with torch.no_grad():
        enc = _rag["tokenizer"]([query], padding=True, truncation=True, max_length=512, return_tensors="pt")
        out = _rag["model"](**enc).last_hidden_state
        mask = enc["attention_mask"].unsqueeze(-1).float()
        emb = (out * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
        emb = torch.nn.functional.normalize(emb, p=2, dim=1)
    return emb.numpy()[0]


# ---------------------------------------------------------------------------
# 查询日志（旁路记录，不影响检索行为）
# ---------------------------------------------------------------------------


def _log_query(query: str, total: int, top1_score: float, latency_ms: float) -> None:
    """记录一次检索：内存环形缓冲 + 可选落盘（PVG_QUERY_LOG）；任何异常都不外抛。"""
    try:
        rec = {
            "ts": datetime.now(CST).isoformat(),
            "query": query,
            "total": total,
            "top1_score": round(float(top1_score), 6),
            "hit": total > 0 and top1_score >= HIT_THRESHOLD,
            "latency_ms": round(latency_ms, 1),
        }
        _query_log.append(rec)
        if QUERY_LOG_PATH:
            with open(QUERY_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 日志是旁路，绝不能影响检索
        pass


# ---------------------------------------------------------------------------
# 文档浏览辅助（/docs）
# ---------------------------------------------------------------------------


def _doc_id(doc: dict[str, Any]) -> str:
    """文档 id：title 的 sha1 前 12 位（确定性计算，不依赖文件中的 id 字段）。"""
    return hashlib.sha1((doc.get("title") or "").encode("utf-8")).hexdigest()[:12]


def _parse_dt(value: Any) -> Optional[datetime]:
    """容错解析 ISO 时间；缺失或非法返回 None。无 tzinfo 时按东八区处理。"""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=CST)


def _valid_state(doc: dict[str, Any], now: datetime) -> str:
    """生效状态：expired（已过期）/ expiring（7 天内到期）/ valid（生效中）。

    valid_until 缺失或不可解析时按 valid 处理（容错缺失字段）。
    """
    until = _parse_dt(doc.get("valid_until"))
    if until is None:
        return "valid"
    if until < now:
        return "expired"
    if until <= now + timedelta(days=EXPIRING_DAYS):
        return "expiring"
    return "valid"


def _doc_view(idx: int, score: float, rank: int) -> dict[str, Any]:
    d = _rag["docs"][idx]
    return {
        "rank": rank,
        "score": round(float(score), 4),
        "title": d["title"],
        "knowledge_type": d["knowledge_type"],
        "valid_until": d["valid_until"],
        "content": d["text"][:600],
    }


def hybrid_search(query: str, count: int = 5) -> dict[str, Any]:
    """BM25 + 向量，RRF 融合；返回融合结果与各阶段明细（供 /search 与 /stages 复用）。"""
    _load_rag()
    np = _rag["np"]

    bm25_scores = _rag["bm25"].get_scores(list(_rag["jieba"].cut_for_search(query)))
    bm25_order = np.argsort(bm25_scores)[::-1][:BM25_TOP]

    vec_scores = _rag["embeddings"] @ _embed_query(query)
    vec_order = np.argsort(vec_scores)[::-1][:VEC_TOP]

    rrf: dict[int, float] = {}
    for rank, idx in enumerate(bm25_order):
        rrf[int(idx)] = rrf.get(int(idx), 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, idx in enumerate(vec_order):
        rrf[int(idx)] = rrf.get(int(idx), 0.0) + 1.0 / (RRF_K + rank + 1)

    fused_order = sorted(rrf, key=rrf.get, reverse=True)
    return {
        "bm25": [(_idx, bm25_scores[_idx]) for _idx in bm25_order],
        "vector": [(_idx, vec_scores[_idx]) for _idx in vec_order],
        "fused": [(_idx, rrf[_idx]) for _idx in fused_order],
        "count": count,
    }


# Swagger UI 移到 /apidocs：/docs 留给知识文档浏览接口
app = FastAPI(title="pvg-rag-service", version="1.0.0", docs_url="/apidocs", redoc_url=None)


class SearchRequest(BaseModel):
    query: str
    count: Optional[int] = Field(default=5, ge=1, le=20)


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        _load_rag()
        return {"status": "ok", "docs": len(_rag["docs"]), "index_dir": str(KNOWLEDGE_INDEX_DIR)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "detail": str(e)}


@app.post("/search")
def search(req: SearchRequest) -> dict[str, Any]:
    """生产检索接口——与 pvg_knowledge_search 的返回结构保持一致。"""
    started = time.monotonic()
    stages = hybrid_search(req.query, req.count or 5)
    results = []
    for idx, score in stages["fused"][: (req.count or 5)]:
        d = _rag["docs"][idx]
        results.append({
            "title": d["title"],
            "knowledge_type": d["knowledge_type"],
            "valid_until": d["valid_until"],
            "content": d["text"][:1200],
            "scores": {"rrf": round(float(score), 4)},
        })
    top1 = float(stages["fused"][0][1]) if stages["fused"] else 0.0
    _log_query(req.query, len(results), top1, (time.monotonic() - started) * 1000)
    return {
        "query": req.query,
        "results": results,
        "total": len(results),
        "canned_response_fields": ["knowledge_answer", "knowledge_source"],
    }


@app.post("/stages")
def stages(req: SearchRequest) -> dict[str, Any]:
    """分阶段召回（FR-804 召回测试台）：BM25 / 向量 / RRF 各阶段 top N 与得分。"""
    started = time.monotonic()
    n = req.count or 5
    s = hybrid_search(req.query, n)
    rrf_rows = s["fused"][:n]
    top1 = float(rrf_rows[0][1]) if rrf_rows else 0.0
    _log_query(req.query, len(rrf_rows), top1, (time.monotonic() - started) * 1000)
    return {
        "query": req.query,
        "params": {"bm25_top": BM25_TOP, "vec_top": VEC_TOP, "rrf_k": RRF_K},
        "bm25": [_doc_view(i, sc, r + 1) for r, (i, sc) in enumerate(s["bm25"][:n])],
        "vector": [_doc_view(i, sc, r + 1) for r, (i, sc) in enumerate(s["vector"][:n])],
        "rrf": [_doc_view(i, sc, r + 1) for r, (i, sc) in enumerate(rrf_rows)],
    }


@app.post("/reload")
def reload() -> dict[str, Any]:
    """强制热加载索引（重建索引后调用，无需重启服务）。"""
    _rag["mtime"] = None
    _rag["index_mtime"] = None
    _load_rag()
    return {"status": "ok", "docs": len(_rag["docs"])}


_reindex_lock = threading.Lock()  # 防并发重建（构建吃 CPU/内存，并发无意义）


@app.post("/reindex")
def reindex() -> dict[str, Any]:
    """重建索引并热加载：子进程跑 build_knowledge_index.py，完成后自动 reload。

    同步执行（约 1-3 分钟，调用方超时放宽）；重复调用 409 防并发重建。
    """
    if not _reindex_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="索引重建任务正在进行中")
    started = time.monotonic()
    try:
        env = dict(os.environ)
        env["PVG_KNOWLEDGE_INDEX"] = str(KNOWLEDGE_INDEX_DIR)
        if BUNDLES_DIR:
            env["PVG_BUNDLES_DIR"] = BUNDLES_DIR
        proc = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=REINDEX_TIMEOUT_S,
            env=env,
            cwd=str(BUILD_SCRIPT.parent.parent),
        )
        log_tail = (proc.stdout + proc.stderr).strip().splitlines()[-10:]
        duration = round(time.monotonic() - started, 1)
        if proc.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={"status": "failed", "duration_s": duration, "log_tail": log_tail},
            )
        result = reload()  # 复用现有热加载逻辑（mtime 置空触发重建）
        return {
            "status": "completed",
            "docs": result["docs"],
            "duration_s": duration,
            "log_tail": log_tail,
        }
    finally:
        _reindex_lock.release()


# ---------------------------------------------------------------------------
# 文档浏览（/docs）— 管理后台知识库管理数据源；轻量加载，不触发模型
# ---------------------------------------------------------------------------


@app.get("/docs")
def list_docs(
    q: Optional[str] = Query(default=None, description="标题/正文包含过滤（简单包含，不走向量）"),
    knowledge_type: Optional[str] = Query(default=None),
    valid_state: Optional[str] = Query(default=None, description="valid / expiring / expired"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """文档浏览：分页 + 模糊过滤 + 生效状态过滤。"""
    _load_docs()
    now = datetime.now(CST)
    items = []
    for d in _rag["docs"]:
        if q:
            needle = q.strip().lower()
            if needle and needle not in (d.get("title") or "").lower() \
                    and needle not in (d.get("text") or "").lower():
                continue
        if knowledge_type and d.get("knowledge_type") != knowledge_type:
            continue
        state = _valid_state(d, now)
        if valid_state and state != valid_state:
            continue
        items.append({
            "id": _doc_id(d),
            "title": d.get("title"),
            "knowledge_type": d.get("knowledge_type"),
            "valid_from": d.get("valid_from"),  # 容错：文件中可能无此字段
            "valid_until": d.get("valid_until"),
            "valid_state": state,
            "snippet": (d.get("text") or "")[:150],
        })
    total = len(items)
    start = (page - 1) * page_size
    return {"total": total, "page": page, "page_size": page_size, "items": items[start:start + page_size]}


@app.get("/docs/{doc_id}")
def get_doc(doc_id: str) -> dict[str, Any]:
    """单篇文档完整字段（text 全文）。"""
    _load_docs()
    for d in _rag["docs"]:
        if _doc_id(d) == doc_id:
            return {
                **{k: v for k, v in d.items() if k != "tokens"},  # tokens 为索引中间产物，不返回
                "id": _doc_id(d),
                "valid_state": _valid_state(d, datetime.now(CST)),
            }
    raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")


# ---------------------------------------------------------------------------
# 知识缺口看板（/gaps）— 聚合查询日志中 hit=false 的 query
# ---------------------------------------------------------------------------


def _normalize_query(query: str) -> str:
    """归一化：去全部空白转小写（"行李 托运" 与 "行李托运" 归并）。"""
    return "".join(query.split()).lower()


def _window_records(days: int) -> list[dict[str, Any]]:
    """取窗口内的查询日志（ts 为东八区 ISO 串，直接解析比较）。"""
    cutoff = datetime.now(CST) - timedelta(days=days)
    records = []
    for rec in _query_log:
        ts = _parse_dt(rec.get("ts"))
        if ts is not None and ts >= cutoff:
            records.append(rec)
    return records


@app.get("/gaps")
def gaps(
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """知识缺口看板：窗口内未命中 query 按归一化聚合，次数倒序。"""
    agg: dict[str, dict[str, Any]] = {}
    for rec in _window_records(days):
        if rec.get("hit"):
            continue
        key = _normalize_query(rec.get("query") or "")
        if not key:
            continue
        slot = agg.setdefault(key, {"query": key, "count": 0, "last_seen": rec["ts"], "example_top1_score": 0.0})
        slot["count"] += 1
        slot["last_seen"] = max(slot["last_seen"], rec["ts"])
        # example_top1_score 取最接近命中的一次（最大分），便于判断"差一点就命中"
        slot["example_top1_score"] = max(slot["example_top1_score"], rec.get("top1_score") or 0.0)
    items = sorted(agg.values(), key=lambda x: (-x["count"], x["query"]))[:limit]
    return {"days": days, "items": items, "total": len(agg)}


@app.get("/gaps/stats")
def gaps_stats(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
    """缺口统计：窗口内查询总量、未命中量与占比。"""
    records = _window_records(days)
    total = len(records)
    no_hit = sum(1 for r in records if not r.get("hit"))
    return {
        "window_days": days,
        "total_queries": total,
        "no_hit_count": no_hit,
        "no_hit_ratio": round(no_hit / total, 4) if total else 0.0,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("PVG_RAG_HOST", "127.0.0.1"),
        port=int(os.environ.get("PVG_RAG_PORT", "8901")),
        log_level="info",
    )
