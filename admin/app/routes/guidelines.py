"""规则中心（Guideline）：列表透传 + 起草/修改走审批流（不直接写上游）。

Parlant 字段契约（已核实）：condition/action?/description?/title?/
criticality(low|medium|high)/metadata?/enabled?/tags?[]/
composition_mode?(fluid|canned_fluid|composited_canned|strict_canned)/priority?；
PATCH 另支持 metadata:{set,unset}、tool_associations:{add,remove}、tags:{add,remove}。
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

router = APIRouter(prefix="/api/guidelines", tags=["guidelines"])


class GuidelineDraft(BaseModel):
    """guideline 起草载荷：condition 必填，其余按 Parlant 契约透传。"""

    model_config = ConfigDict(extra="allow")

    condition: str
    action: str | None = None
    criticality: str | None = None
    composition_mode: str | None = None

    def upstream_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")


class GuidelinePatch(BaseModel):
    """guideline 修改载荷：任意 Parlant PATCH 字段，走审批流生效。"""

    model_config = ConfigDict(extra="allow")

    def upstream_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")


@router.get("")
def list_guidelines(
    user: SessionUser = Depends(get_current_user),
    parlant: ParlantClient = Depends(get_parlant),
) -> Any:
    """透传 Parlant guideline 列表（五角色均可读）。"""
    return parlant.list_guidelines()


@router.post("/draft", status_code=201)
def draft_guideline(
    body: GuidelineDraft,
    user: SessionUser = Depends(require_roles("operator", "admin")),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """运营起草新 guideline：存审批单（pending_review），不直接写上游。"""
    doc = create_draft(
        store,
        kind="guideline",
        action="create",
        payload=body.upstream_payload(),
        author=user,
    )
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="guideline.draft",
        target=doc["id"],
        after={"kind": "guideline", "payload": doc["payload"]},
    )
    return doc


@router.patch("/{guideline_id}", status_code=201)
def patch_guideline(
    guideline_id: str,
    body: GuidelinePatch,
    user: SessionUser = Depends(require_roles("operator", "admin")),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """修改 guideline：先取上游原值（供 diff），修改经审批流生效，不是直接改。"""
    before = parlant.get_guideline(guideline_id)
    doc = create_draft(
        store,
        kind="guideline",
        action="update",
        target_id=guideline_id,
        payload=body.upstream_payload(),
        author=user,
        before=before,
    )
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="guideline.draft_update",
        target=doc["id"],
        before=before,
        after={"guideline_id": guideline_id, "payload": doc["payload"]},
    )
    return doc
