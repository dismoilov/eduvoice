"""Citizens: who called, what they asked before, and how to reach them again."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from crm import repo
from crm.deps import current_user, get_db, page, redirect
from crm.telephony import call_citizen
from crm.views.common import require_csrf

router = APIRouter(prefix="/contacts")


@router.get("")
def contact_list(
    request: Request,
    q: str = "",
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    return page(request, "contacts/list.html", user, contacts=repo.contacts(db, q), q=q)


@router.get("/{contact_id}")
def contact_detail(
    contact_id: int,
    request: Request,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    contact = repo.contact(db, contact_id)
    if contact is None:
        raise HTTPException(404, "no contact")
    return page(
        request,
        "contacts/detail.html",
        user,
        contact=contact,
        calls=repo.calls(db, contact_id=contact_id, limit=50),
        tickets=repo.tickets(db, contact_id=contact_id, limit=50),
        callbacks=repo.callbacks(db, pending=False),
        kinds=("applicant", "student", "parent", "other"),
        notice=request.query_params.get("notice", ""),
    )


@router.post("/{contact_id}")
def contact_update(
    contact_id: int,
    request: Request,
    name: str = Form(""),
    kind: str = Form(""),
    university: str = Form(""),
    note: str = Form(""),
    csrf: str = Form(""),
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf)
    repo.update_contact(db, contact_id, name=name, kind=kind, university=university, note=note)
    repo.audit(
        db,
        user_id=int(user["id"]),
        login=user["login"],
        action="contact_update",
        entity="contact",
        entity_id=str(contact_id),
    )
    return redirect(f"/contacts/{contact_id}")


@router.post("/{contact_id}/call")
def contact_call(
    contact_id: int,
    request: Request,
    csrf: str = Form(""),
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Click to call: Asterisk rings the operator first, then dials the citizen."""
    require_csrf(request, csrf)
    contact = repo.contact(db, contact_id)
    if contact is None:
        raise HTTPException(404, "no contact")
    ok = call_citizen(extension=user["extension"], phone=contact["phone"])
    repo.audit(
        db,
        user_id=int(user["id"]),
        login=user["login"],
        action="click_to_call" if ok else "click_to_call_failed",
        entity="contact",
        entity_id=str(contact_id),
        detail=contact["phone"],
    )
    return redirect(
        f"/contacts/{contact_id}?notice={'call_back_started' if ok else 'call_back_failed'}"
    )


@router.post("/{contact_id}/callback")
def contact_callback(
    contact_id: int,
    request: Request,
    due_at: str = Form(""),
    csrf: str = Form(""),
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf)
    when = due_at or (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    repo.create_callback(db, contact_id=contact_id, due_at=when, assignee_id=int(user["id"]))
    return redirect(f"/contacts/{contact_id}")
