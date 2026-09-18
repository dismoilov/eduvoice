"""Small JSON endpoints for the screens that update themselves.

Only what a page needs to refresh without being reloaded: the dashboard figures and
whether the voice assistant is alive. Everything here requires a session, like the rest
of the CRM.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from crm import stats
from crm.config import settings
from crm.deps import current_user, get_db

router = APIRouter(prefix="/api")

_health_cache: dict[str, Any] = {"at": 0.0, "value": None}
HEALTH_TTL_S = 5.0


def bridge_health() -> dict[str, Any] | None:
    """Asks the bridge how it feels, at most once every few seconds.

    Cached on purpose: without it every dashboard render would wait for a network
    timeout whenever the bridge is down — exactly when the page must stay fast.
    """
    if time.monotonic() - _health_cache["at"] < HEALTH_TTL_S:
        return _health_cache["value"]
    value: dict[str, Any] | None = None
    try:
        response = httpx.get(settings.bridge_health_url, timeout=0.6)
        if response.status_code == 200:
            value = response.json()
    except Exception:
        value = None
    _health_cache.update(at=time.monotonic(), value=value)
    return value


@router.get("/dashboard")
def dashboard_data(
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    figures = stats.dashboard(db, int(user["id"]))
    bridge = bridge_health()
    return {
        "figures": figures,
        "bridge": {"up": bridge is not None, "active_calls": (bridge or {}).get("active_calls", 0)},
        "at": time.strftime("%H:%M:%S"),
    }
