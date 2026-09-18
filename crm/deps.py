"""Shared plumbing for the routes: database, current user, roles, templates.

Everything a page needs is assembled here so that each view can be read as "what this
screen does" and nothing else.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from crm import repo
from crm.config import settings
from crm.i18n import DEFAULT_LANGUAGE, LANGUAGES, translator
from crm.security import SESSION_COOKIE, csrf_token, read_session
from store.db import connect

LANG_COOKIE = "eduvoice_lang"
THEME_COOKIE = "eduvoice_theme"


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection per request: SQLite is fast to open and this avoids sharing state."""
    connection = connect(request.app.state.db_path)
    try:
        yield connection
    finally:
        connection.close()


def language_of(request: Request) -> str:
    value = request.cookies.get(LANG_COOKIE, DEFAULT_LANGUAGE)
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def theme_of(request: Request) -> str:
    return "dark" if request.cookies.get(THEME_COOKIE) == "dark" else "light"


class NotLoggedIn(Exception):
    """Raised instead of returning a page, so the app can send the visitor to the login."""


def current_user(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    session = read_session(
        request.app.state.session_secret,
        request.cookies.get(SESSION_COOKIE),
        settings.session_hours * 3600,
    )
    if session is None:
        raise NotLoggedIn
    user = repo.user(db, session.user_id)
    if user is None or not user["active"]:
        raise NotLoggedIn
    return user


def require_roles(*roles: str):
    """Guards a screen: `Depends(require_roles("supervisor", "admin"))`."""

    def guard(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if user["role"] not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no_access")
        return user

    return guard


def can(user: dict[str, Any], *roles: str) -> bool:
    return user["role"] in roles


# ------------------------------------------------------------------- templates

templates = Jinja2Templates(directory=str(settings.templates_dir))


def page(
    request: Request,
    name: str,
    user: dict[str, Any] | None = None,
    status_code: int = 200,
    **context: Any,
):
    """Renders a template with everything the layout expects."""
    language = language_of(request)
    return templates.TemplateResponse(
        request,
        name,
        {
            "t": translator(language),
            "lang": language,
            "theme": theme_of(request),
            "user": user,
            "can": can,
            "csrf": csrf_token(
                request.app.state.session_secret, request.cookies.get(SESSION_COOKIE)
            ),
            "path": request.url.path,
            "query": dict(request.query_params),
            **context,
        },
        status_code=status_code,
    )


def redirect(url: str) -> RedirectResponse:
    """A redirect after a form post, so a refresh does not repeat the action."""
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
