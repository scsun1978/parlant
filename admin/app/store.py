"""治理数据存储抽象：MongoDB 可用时用 Mongo，否则降级进程内内存实现。

治理数据（审批单、审计日志）按设计方案 v2.1 §6 落 MongoDB（ADR-0004）；
本地开发与测试不依赖真 Mongo——``MONGO_URL`` 未设置或连接失败时
自动降级为进程内 dict 实现。pymongo 为惰性 import，未安装不影响内存模式。
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Protocol

from admin.app.config import Settings

logger = logging.getLogger("admin.store")

Filter = dict[str, Any]


def _match(doc: dict[str, Any], filters: Filter) -> bool:
    """内存过滤：支持等值与 {$gte, $lte} 区间算子（ISO 时间串按字典序比较）。"""
    for key, expected in filters.items():
        actual = doc.get(key)
        if isinstance(expected, dict):
            if "$gte" in expected and not (actual is not None and actual >= expected["$gte"]):
                return False
            if "$lte" in expected and not (actual is not None and actual <= expected["$lte"]):
                return False
        elif actual != expected:
            return False
    return True


class Store(Protocol):
    """存储协议：面向"集合 + 文档"的最小读写面。"""

    name: str

    def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]:
        """插入文档，自动补 ``id`` 字段并返回完整文档。"""
        ...

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        """按 id 取文档，不存在返回 None。"""
        ...

    def update(self, collection: str, doc_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        """按 id 局部更新（仅限审批单状态流转；审计日志不得调用本方法）。"""
        ...

    def find(
        self,
        collection: str,
        filters: Filter | None = None,
        *,
        offset: int = 0,
        limit: int = 50,
        sort: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """按过滤条件分页查询，返回 (items, total)；sort 形如 "-ts" 表示倒序。"""
        ...

    def ping(self) -> bool:
        """健康探测。"""
        ...


class InMemoryStore:
    """进程内 dict 存储：测试与本地开发用，重启即清空。"""

    name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]:
        doc = dict(doc)
        doc.setdefault("id", uuid.uuid4().hex)
        with self._lock:
            self._data.setdefault(collection, {})[doc["id"]] = doc
        return dict(doc)

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._data.get(collection, {}).get(doc_id)
        return dict(doc) if doc is not None else None

    def update(self, collection: str, doc_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            doc = self._data.get(collection, {}).get(doc_id)
            if doc is None:
                return None
            doc.update(patch)
            return dict(doc)

    def find(
        self,
        collection: str,
        filters: Filter | None = None,
        *,
        offset: int = 0,
        limit: int = 50,
        sort: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            docs = [dict(d) for d in self._data.get(collection, {}).values()]
        if filters:
            docs = [d for d in docs if _match(d, filters)]
        if sort:
            reverse = sort.startswith("-")
            key = sort.lstrip("-")
            docs.sort(key=lambda d: d.get(key) or "", reverse=reverse)
        total = len(docs)
        return docs[offset : offset + limit], total

    def ping(self) -> bool:
        return True


class MongoStore:
    """MongoDB 存储：pymongo 惰性 import，构造时做连通性探测。"""

    name = "mongo"

    def __init__(self, mongo_url: str, db_name: str) -> None:
        from pymongo import MongoClient  # 惰性 import：内存模式不强制依赖

        self._client: Any = MongoClient(mongo_url, serverSelectionTimeoutMS=2000)
        self._client.admin.command("ping")  # 连接失败抛异常，由工厂降级
        self._db = self._client[db_name]

    def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]:
        doc = dict(doc)
        doc.setdefault("id", uuid.uuid4().hex)
        doc["_id"] = doc["id"]
        self._db[collection].insert_one(doc)
        doc.pop("_id", None)
        return doc

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = self._db[collection].find_one({"_id": doc_id}, {"_id": 0})
        return doc

    def update(self, collection: str, doc_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        self._db[collection].update_one({"_id": doc_id}, {"$set": patch})
        return self.get(collection, doc_id)

    def find(
        self,
        collection: str,
        filters: Filter | None = None,
        *,
        offset: int = 0,
        limit: int = 50,
        sort: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        cursor = self._db[collection].find(filters or {}, {"_id": 0})
        if sort:
            direction = -1 if sort.startswith("-") else 1
            cursor = cursor.sort(sort.lstrip("-"), direction)
        total = self._db[collection].count_documents(filters or {})
        return list(cursor.skip(offset).limit(limit)), total

    def ping(self) -> bool:
        try:
            self._client.admin.command("ping")
            return True
        except Exception:
            return False


def create_store(settings: Settings) -> Store:
    """工厂：MONGO_URL 可用则用 Mongo，否则降级内存实现并打警告日志。"""
    if settings.mongo_url:
        try:
            return MongoStore(settings.mongo_url, settings.mongo_db)
        except Exception as exc:  # 含 ImportError（未装 pymongo）与连接失败
            logger.warning("MongoDB 不可用（%s），治理数据降级为进程内内存存储", exc)
    else:
        logger.info("MONGO_URL 未设置，治理数据使用进程内内存存储")
    return InMemoryStore()
