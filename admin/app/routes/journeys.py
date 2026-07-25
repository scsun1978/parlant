"""流程设计器（Journey）：切片 1 仅列表透传。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from admin.app.auth import SessionUser, get_current_user
from admin.app.deps import get_parlant
from admin.app.parlant_client import ParlantClient

router = APIRouter(prefix="/api/journeys", tags=["journeys"])


@router.get("")
def list_journeys(
    user: SessionUser = Depends(get_current_user),
    parlant: ParlantClient = Depends(get_parlant),
) -> Any:
    """透传 Parlant journey 列表（含 id/title/description/triggers，五角色均可读）。"""
    return parlant.list_journeys()
