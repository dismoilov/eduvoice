"""Administration: who works here, and what everybody has been doing."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request

from crm import repo
from crm.deps import get_db, page, redirect, require_roles
from crm.security import hash_password
from crm.views.common import require_csrf

router = APIRouter(prefix="/admin")


@router.get("")
def admin_home(
    request: Request,
    user: dict = Depends(require_roles("admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    return page(
        request,
        "admin.html",
        user,
        users=repo.users(db),
        roles=repo.ROLES,
        audit=repo.audit_log(db, limit=100),
        problem=request.query_params.get("problem", ""),
    )


@router.post("/users")
def create_user(
    request: Request,
    login: str = Form(""),
    name: str = Form(""),
    role: str = Form("operator"),
    extension: str = Form(""),
    password: str = Form(""),
    csrf: str = Form(""),
    user: dict = Depends(require_roles("admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf)
    if not (login.strip() and password and role in repo.ROLES):
        return redirect("/admin?problem=incomplete")
    if repo.user_exists(db, login):
        # The login column is unique; without this check the insert raised a bare 500.
        return redirect("/admin?problem=duplicate")
    new_id = repo.create_user(db, login, name or login, role, hash_password(password), extension)
    repo.audit(
        db,
        user_id=int(user["id"]),
        login=user["login"],
        action="user_create",
        entity="user",
        entity_id=str(new_id),
        detail=login,
    )
    return redirect("/admin")


@router.post("/users/{user_id}")
def update_user(
    user_id: int,
    request: Request,
    name: str = Form(""),
    role: str = Form(""),
    extension: str = Form(""),
    active: str = Form(""),
    password: str = Form(""),
    csrf: str = Form(""),
    user: dict = Depends(require_roles("admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf)
    fields: dict = {"active": 1 if active else 0}
    if name.strip():
        fields["name"] = name.strip()
    if role in repo.ROLES:
        fields["role"] = role
    fields["extension"] = extension.strip()
    if password:
        fields["password_hash"] = hash_password(password)
    repo.update_user(db, user_id, **fields)
    repo.audit(
        db,
        user_id=int(user["id"]),
        login=user["login"],
        action="user_update",
        entity="user",
        entity_id=str(user_id),
    )
    return redirect("/admin")
