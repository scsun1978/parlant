"""话术中心（Canned Response）：与 guideline 同构的审批流结构。

Parlant 字段契约（已核实）：value/fields:[{name,description,examples[]}]/
signals?[]/tags?[]/metadata?[]/field_dependencies?[]。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from admin.app import audit
from admin.app.auth import SessionUser, get_current_user, require_roles
from admin.app.deps import get_parlant, get_store
from admin.app.parlant_client import ParlantClient
from admin.app.routes.approvals import create_draft
from admin.app.store import Store

router = APIRouter(prefix="/api/canned-responses", tags=["canned-responses"])

# 预览端点按规格独立挂载 /api/canned/preview
preview_router = APIRouter(prefix="/api/canned", tags=["canned-responses"])


class CannedResponseDraft(BaseModel):
    """话术模板起草载荷：value 必填，fields 默认空，其余按契约透传。"""

    model_config = ConfigDict(extra="allow")

    value: str
    fields: list[dict[str, Any]] = []

    def upstream_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")


class CannedPreviewRequest(BaseModel):
    """话术渲染预览请求：模板 + 槽位样例值。"""

    value: str
    sample_fields: dict[str, Any] = {}


@preview_router.post("/preview")
def preview_canned_response(
    body: CannedPreviewRequest,
    user: SessionUser = Depends(require_roles("operator", "reviewer", "admin")),
) -> dict[str, Any]:
    """jinja2 渲染预览（StrictUndefined：槽位悬空即报错，防止线上模板不可选）。

    jinja2 为 FastAPI 自带依赖，此处惰性 import 仅为保持模块加载轻量。
    """
    from jinja2 import Environment, StrictUndefined

    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        rendered = env.from_string(body.value).render(**body.sample_fields)
    except Exception as exc:
        return {"rendered": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"rendered": rendered, "error": None}


@router.get("")
def list_canned_responses(
    user: SessionUser = Depends(get_current_user),
    parlant: ParlantClient = Depends(get_parlant),
) -> Any:
    """透传 Parlant canned response 列表（五角色均可读）。"""
    return parlant.list_canned_responses()


@router.post("/draft", status_code=201)
def draft_canned_response(
    body: CannedResponseDraft,
    user: SessionUser = Depends(require_roles("operator", "admin")),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """运营起草话术模板：存审批单（pending_review），审核通过才写入上游。"""
    doc = create_draft(
        store,
        kind="canned_response",
        action="create",
        payload=body.upstream_payload(),
        author=user,
    )
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="canned.draft",
        target=doc["id"],
        after={"kind": "canned_response", "payload": doc["payload"]},
    )
    return doc
