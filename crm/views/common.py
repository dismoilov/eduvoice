"""Small helpers shared by the form-handling views."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from crm.security import SESSION_COOKIE, csrf_ok


def require_csrf(request: Request, token: str) -> None:
    """Every form post carries a token tied to the session cookie."""
    if not csrf_ok(request.app.state.session_secret, request.cookies.get(SESSION_COOKIE), token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad token")
