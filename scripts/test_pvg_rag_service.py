"""pvg_rag_service 新增能力测试：/docs 浏览、查询日志旁路、/gaps 缺口聚合。

构造临时小 corpus（3 篇假 docs.jsonl），monkeypatch 索引目录与 hybrid_search，
避开真实索引文件与 jina 模型加载（重）；/search /stages 的日志旁路经
monkeypatch 的 hybrid_search 验证，既不断言检索质量也不触重型依赖。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ 非包，直接按模块导入

import pvg_rag_service as svc

CST = timezone(timedelta(hours=8))
NOW = datetime.now(CST)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_corpus(tmp_path: Path) -> list[dict[str, Any]]:
    """3 篇假文档：生效中 / 7 天内到期 / 已过期。"""
    docs = [
        {
            "id": "x1",
            "title": "行李托运限额是多少",
            "knowledge_type": "policy",
            "valid_until": _iso(NOW + timedelta(days=90)),
            "text": "国内航班经济舱免费托运行李额为 20 公斤。" * 10,
            "tokens": ["行李", "托运"],
        },
        {
            "id": "x2",
            "title": "T2 到 S2 捷运怎么坐",
            "knowledge_type": "service_information",
            "valid_until": _iso(NOW + timedelta(days=3)),  # expiring
            "text": "T2 航站楼至 S2 卫星厅可乘捷运列车，车程约 3 分钟。",
            "tokens": ["捷运"],
        },
        {
            "id": "x3",
            "title": "临时停车优惠公告",
            "knowledge_type": "announcement",
            "valid_until": _iso(NOW - timedelta(days=1)),  # expired
            "text": "停车优惠已结束。",
            "tokens": ["停车"],
        },
    ]
    with open(tmp_path / "docs.jsonl", "w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return docs


@pytest.fixture()
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """临时 corpus + 重置服务全局状态的 TestClient。"""
    docs = _make_corpus(tmp_path)
    monkeypatch.setattr(svc, "KNOWLEDGE_INDEX_DIR", tmp_path)
    monkeypatch.setattr(svc, "QUERY_LOG_PATH", None)
    svc._rag["docs"] = None
    svc._rag["mtime"] = None
    svc._rag["index_mtime"] = None
    svc._query_log.clear()
    yield TestClient(svc.app)
    svc._rag["docs"] = None
    svc._rag["mtime"] = None
    svc._query_log.clear()


def _fake_hybrid(docs_titles: list[str]) -> dict[str, Any]:
    """固定 hybrid_search 返回：doc0 高分命中或空结果两种形态在测试中再加工。"""
    return {
        "bm25": [(0, 3.2)],
        "vector": [(0, 0.88)],
        "fused": [(0, 0.031)],
        "count": 5,
    }


# ---------------------------------------------------------------------------
# /docs
# ---------------------------------------------------------------------------


def test_docs_list_pagination_and_fields(corpus: TestClient) -> None:
    body = corpus.get("/docs", params={"page_size": 2, "page": 1}).json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) == 2
    item = body["items"][0]
    assert set(item) == {"id", "title", "knowledge_type", "valid_from", "valid_until", "valid_state", "snippet"}
    assert len(item["id"]) == 12
    assert len(item["snippet"]) <= 150
    page2 = corpus.get("/docs", params={"page_size": 2, "page": 2}).json()
    assert len(page2["items"]) == 1


def test_docs_valid_state_classification(corpus: TestClient) -> None:
    items = corpus.get("/docs").json()["items"]
    by_title = {i["title"]: i["valid_state"] for i in items}
    assert by_title["行李托运限额是多少"] == "valid"
    assert by_title["T2 到 S2 捷运怎么坐"] == "expiring"
    assert by_title["临时停车优惠公告"] == "expired"
    # 过滤
    assert corpus.get("/docs", params={"valid_state": "expired"}).json()["total"] == 1
    assert corpus.get("/docs", params={"valid_state": "expiring"}).json()["total"] == 1


def test_docs_q_and_type_filter(corpus: TestClient) -> None:
    body = corpus.get("/docs", params={"q": "捷运"}).json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "T2 到 S2 捷运怎么坐"
    body = corpus.get("/docs", params={"knowledge_type": "policy"}).json()
    assert body["total"] == 1
    assert corpus.get("/docs", params={"q": "不存在的词"}).json()["total"] == 0


def test_doc_detail_full_text_and_404(corpus: TestClient) -> None:
    items = corpus.get("/docs").json()["items"]
    doc_id = items[0]["id"]
    detail = corpus.get(f"/docs/{doc_id}").json()
    assert detail["title"] == items[0]["title"]
    assert len(detail["text"]) > 150  # 全文返回
    assert "tokens" not in detail  # 索引中间产物不外泄
    assert detail["valid_state"] == "valid"
    assert corpus.get("/docs/nonexistent12").status_code == 404


# ---------------------------------------------------------------------------
# 查询日志旁路
# ---------------------------------------------------------------------------


def test_search_logs_query_with_hit(corpus: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    svc._load_docs()  # hybrid_search 被 patch，需先让 _rag["docs"] 就位（检索结果拼装依赖）
    monkeypatch.setattr(svc, "hybrid_search", lambda q, c: _fake_hybrid([]))
    body = corpus.post("/search", json={"query": "行李托运"}).json()
    assert body["total"] == 1  # 既有返回行为不变
    assert len(svc._query_log) == 1
    rec = svc._query_log[0]
    assert rec["query"] == "行李托运"
    assert rec["total"] == 1
    assert rec["top1_score"] == pytest.approx(0.031)
    assert rec["hit"] is True
    assert rec["latency_ms"] >= 0


def test_stages_logs_query_without_hit(corpus: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "hybrid_search", lambda q, c: {"bm25": [], "vector": [], "fused": [], "count": c})
    corpus.post("/stages", json={"query": "冷门问题", "count": 3})
    rec = svc._query_log[0]
    assert rec["total"] == 0
    assert rec["hit"] is False


def test_query_log_file_sink(corpus: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log_file = tmp_path / "qlog.jsonl"
    monkeypatch.setattr(svc, "QUERY_LOG_PATH", str(log_file))
    svc._log_query("直接调用", 1, 0.05, 3.2)
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["query"] == "直接调用"


# ---------------------------------------------------------------------------
# /gaps
# ---------------------------------------------------------------------------


def _seed_log() -> None:
    svc._log_query("T2 到 S2 怎么走", 0, 0.0, 5.0)
    svc._log_query("t2到s2怎么走", 0, 0.004, 5.0)  # 归一化后与上一条归并
    svc._log_query("宠物可以托运吗", 0, 0.008, 5.0)
    svc._log_query("行李托运", 1, 0.05, 5.0)  # hit=true，不进缺口


def test_gaps_aggregation(corpus: TestClient) -> None:
    _seed_log()
    body = corpus.get("/gaps").json()
    assert body["total"] == 2
    items = body["items"]
    assert items[0]["query"] == "t2到s2怎么走"  # 归一化（去空格转小写）聚合，2 次居首
    assert items[0]["count"] == 2
    assert items[0]["example_top1_score"] == pytest.approx(0.004)  # 取最接近命中的一次
    assert items[0]["last_seen"]
    assert items[1]["query"] == "宠物可以托运吗"
    assert items[1]["count"] == 1
    # limit 生效
    assert len(corpus.get("/gaps", params={"limit": 1}).json()["items"]) == 1


def test_gaps_stats(corpus: TestClient) -> None:
    _seed_log()
    stats = corpus.get("/gaps/stats").json()
    assert stats["window_days"] == 7
    assert stats["total_queries"] == 4
    assert stats["no_hit_count"] == 3
    assert stats["no_hit_ratio"] == pytest.approx(0.75)


def test_gaps_window_filter(corpus: TestClient) -> None:
    """窗口外（>days）的旧记录不参与聚合。"""
    old = {
        "ts": (NOW - timedelta(days=30)).isoformat(),
        "query": "很久以前的问题",
        "total": 0,
        "top1_score": 0.0,
        "hit": False,
        "latency_ms": 1.0,
    }
    svc._query_log.append(old)
    _seed_log()
    body = corpus.get("/gaps", params={"days": 7}).json()
    assert all("很久" not in i["query"] for i in body["items"])
    stats = corpus.get("/gaps/stats", params={"days": 31}).json()
    assert stats["total_queries"] == 5  # 31 天窗口含旧记录
