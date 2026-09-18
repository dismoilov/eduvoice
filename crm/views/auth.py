"""Signing in and out, and the two switches everybody needs: language and theme."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request

from crm import repo
from crm.config import settings
from crm.deps import LANG_COOKIE, THEME_COOKIE, get_db, page, redirect, safe_path
from crm.i18n import LANGUAGES
from crm.security import SESSION_COOKIE, LoginThrottle, csrf_ok, make_session, verify_password

router = APIRouter()
throttle = LoginThrottle(settings.login_attempts, settings.login_block_s)


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return page(request, "login.html", None, next=safe_path(next), error="")


@router.post("/login")
def login(
    request: Request,
    login: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
):
    if not csrf_ok(request.app.state.session_secret, request.cookies.get(SESSION_COOKIE), csrf):
        # Without this another site could log an operator into an account it controls.
        return page(request, "login.html", None, next=safe_path(next), error="login_failed")

    blocked = throttle.blocked_for(login)
    if blocked:
        return page(
            request,
            "login.html",
            None,
            next=safe_path(next),
            error="login_blocked",
            seconds=blocked,
        )

    user = repo.user_by_login(db, login)
    if user is None or not verify_password(password, user["password_hash"]):
        throttle.failed(login)
        repo.audit(db, user_id=None, login=login, action="login_failed")
        return page(request, "login.html", None, next=safe_path(next), error="login_failed")

    throttle.passed(login)
    repo.audit(db, user_id=user["id"], login=user["login"], action="login")
    response = redirect(safe_path(next))
    response.set_cookie(
        SESSION_COOKIE,
        make_session(request.app.state.session_secret, int(user["id"])),
        max_age=settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    """POST, not GET: an `<img src="/logout">` on any page would log the operator out."""
    if not csrf_ok(request.app.state.session_secret, request.cookies.get(SESSION_COOKIE), csrf):
        return redirect("/")
    response = redirect("/login")
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.post("/prefs")
def preferences(
    request: Request,
    language: str = Form(""),
    theme: str = Form(""),
    back: str = Form("/"),
    csrf: str = Form(""),
):
    """Language and theme live in cookies: no account needed, no page state to keep.

    The token is checked, not merely carried. The form has always sent one and the
    handler ignored it, so another site could have switched an operator's interface to a
    language they do not read in the middle of a shift.
    """
    if not csrf_ok(request.app.state.session_secret, request.cookies.get(SESSION_COOKIE), csrf):
        return redirect("/")
    response = redirect(safe_path(back))
    if language in LANGUAGES:
        response.set_cookie(LANG_COOKIE, language, max_age=365 * 24 * 3600, samesite="lax")
    if theme in ("light", "dark"):
        response.set_cookie(THEME_COOKIE, theme, max_age=365 * 24 * 3600, samesite="lax")
    return response
