"""术语表（Terms）：列表透传；创建为低合规操作，operator 直建 + 审计（无需双人）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from admin.app import audit
from admin.app.auth import SessionUser, get_current_user, require_roles
from admin.app.deps import get_parlant, get_store
from admin.app.parlant_client import ParlantClient
from admin.app.store import Store

router = APIRouter(prefix="/api/terms", tags=["terms"])


class TermCreate(BaseModel):
    """术语创建载荷，按 Parlant 契约：name/description 必填，synonyms/tags 可选。"""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str
    synonyms: list[str] | None = None
    tags: list[str] | None = None

    def upstream_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")


@router.get("")
def list_terms(
    user: SessionUser = Depends(get_current_user),
    parlant: ParlantClient = Depends(get_parlant),
) -> Any:
    """透传 Parlant 术语列表（五角色均可读）。"""
    return parlant.list_terms()


@router.post("", status_code=201)
def create_term(
    body: TermCreate,
    user: SessionUser = Depends(require_roles("operator", "admin")),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> Any:
    """operator 直建术语（非高合规，无需双人复核），写操作追加审计。"""
    result = parlant.create_term(body.upstream_payload())
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="term.create",
        target=result.get("id", "") if isinstance(result, dict) else "",
        after=body.upstream_payload(),
    )
    return result
