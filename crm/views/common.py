"""Small helpers shared by the form-handling views."""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException, Request, status

from crm import repo
from crm.security import SESSION_COOKIE, csrf_ok

# SQLite stores integers as 64-bit; anything larger cannot be a row id and raises
# OverflowError rather than simply finding nothing.
MAX_ROW_ID = 2**63 - 1
# Enough pages for any real history; beyond it SQLite overflows instead of finding nothing.
MAX_PAGE = 10**6


def require_csrf(request: Request, token: str) -> None:
    """Every form post carries a token tied to the session cookie."""
    if not csrf_ok(request.app.state.session_secret, request.cookies.get(SESSION_COOKIE), token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad token")


def as_id(value: str | int | None) -> int | None:
    """A form field that should hold a row id, from a browser that may send anything.

    Returns None for empty, malformed or nonsensical values, which every caller here
    already treats as "not set". Passing the raw text to `int()` turned a stray character
    in a select box into an unhandled 500 with a traceback.
    """
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= MAX_ROW_ID else None


def assignee_id(db: sqlite3.Connection, value: str | int | None) -> int | None:
    """The operator a ticket is being handed to, or None if there is no such person.

    A select box can carry an id that no longer exists — the page was open while the
    account was removed, or the form was replayed. Passing it straight through hit the
    foreign key and surfaced as the "database is unavailable" page, which says nothing
    true about what happened.
    """
    number = as_id(value)
    if number is None:
        return None
    found = repo.user(db, number)
    return number if found and found["active"] else None
