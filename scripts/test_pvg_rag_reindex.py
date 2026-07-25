"""RAG /reindex：子进程参数、完成后 reload、失败 500、并发锁。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import pvg_rag_service as svc

from test_pvg_rag_service import corpus  # noqa: F401 — 复用临时 corpus fixture


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_reindex_runs_build_script_and_reloads(corpus: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        return _FakeProc(0, stdout="parsed 891 valid docs\nindex written to /data/knowledge/index")

    reloaded: list[int] = []
    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    monkeypatch.setattr(svc, "reload", lambda: reloaded.append(1) or {"status": "ok", "docs": 891})
    monkeypatch.setattr(svc, "BUNDLES_DIR", "/data/knowledge/bundles/v2-miniprogram-20260719b")

    body = corpus.post("/reindex").json()
    assert body["status"] == "completed"
    assert body["docs"] == 891
    assert body["duration_s"] >= 0
    assert body["log_tail"][-1].startswith("index written")
    # 子进程调用参数正确：同 python 解释器 + 构建脚本 + env 注入
    assert calls["cmd"][0] == svc.sys.executable
    assert calls["cmd"][1].endswith("build_knowledge_index.py")
    env = calls["kwargs"]["env"]
    assert env["PVG_KNOWLEDGE_INDEX"] == str(svc.KNOWLEDGE_INDEX_DIR)
    assert env["PVG_BUNDLES_DIR"] == "/data/knowledge/bundles/v2-miniprogram-20260719b"
    # 完成后触发 reload 热加载
    assert reloaded == [1]


def test_reindex_failure_returns_500(corpus: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        svc.subprocess, "run", lambda *a, **k: _FakeProc(1, stderr="torch boom")
    )
    reloaded: list[int] = []
    monkeypatch.setattr(svc, "reload", lambda: reloaded.append(1))
    resp = corpus.post("/reindex")
    assert resp.status_code == 500
    assert "torch boom" in str(resp.json()["detail"])
    assert reloaded == []  # 失败不触发 reload


def test_reindex_concurrent_conflict(corpus: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc.subprocess, "run", lambda *a, **k: _FakeProc(0))
    monkeypatch.setattr(svc, "reload", lambda: {"status": "ok", "docs": 1})
    # 人工持锁模拟重建进行中 → 重复调用 409
    assert svc._reindex_lock.acquire(blocking=False)
    try:
        resp = corpus.post("/reindex")
        assert resp.status_code == 409
    finally:
        svc._reindex_lock.release()
    # 释放后可正常执行
    assert corpus.post("/reindex").status_code == 200
