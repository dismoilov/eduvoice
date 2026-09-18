"""The first screen: what is happening right now and what is waiting for me."""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from crm import repo, stats
from crm.config import settings
from crm.deps import current_user, get_db, page

router = APIRouter()


def bridge_is_up() -> dict[str, Any] | None:
    """Asks the voice bridge how it feels. A dead bridge must be visible immediately."""
    try:
        response = httpx.get(settings.bridge_health_url, timeout=1.5)
        return response.json() if response.status_code == 200 else None
    except Exception:
        return None


@router.get("/")
def dashboard(
    request: Request,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    return page(
        request,
        "dashboard.html",
        user,
        figures=stats.dashboard(db, int(user["id"])),
        my_tickets=repo.tickets(db, assignee_id=int(user["id"]), status="open", limit=8),
        unassigned=repo.tickets(db, unassigned=True, status="open", limit=8),
        recent_calls=repo.calls(db, limit=8),
        callbacks=repo.callbacks(db, assignee_id=int(user["id"])),
        bridge=bridge_is_up(),
        week=stats.analytics(db, days=7),
    )
