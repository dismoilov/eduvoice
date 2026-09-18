"""The first screen: what is happening right now and what is waiting for me."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from crm import repo, stats
from crm.deps import current_user, get_db, page
from crm.views.api import bridge_health

router = APIRouter()


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
        today=stats.today(),
        my_tickets=repo.tickets(db, assignee_id=int(user["id"]), status="open", limit=8),
        unassigned=repo.tickets(db, unassigned=True, status="open", limit=8),
        recent_calls=repo.calls(db, limit=8),
        callbacks=repo.callbacks(db, assignee_id=int(user["id"])),
        bridge=bridge_health(),
        week=stats.calls_by_day(db, days=7),
    )
