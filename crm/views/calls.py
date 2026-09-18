"""Calls: the list, one call with its recording and transcript, and the way into a request."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from crm import repo
from crm.config import settings
from crm.deps import current_user, get_db, page, redirect
from crm.views.common import require_csrf

router = APIRouter(prefix="/calls")


@router.get("")
def call_list(
    request: Request,
    q: str = "",
    outcome: str = "",
    day: str = "",
    unhandled: str = "",
    page_no: int = 1,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """One page of calls. Deep history stays reachable, but is never rendered in one go."""
    page_no = max(1, page_no)
    size = 100
    found = repo.calls(
        db,
        query=q,
        outcome=outcome,
        day=day,
        unhandled=bool(unhandled),
        limit=size + 1,  # one extra row tells us whether a next page exists
        offset=(page_no - 1) * size,
    )
    return page(
        request,
        "calls/list.html",
        user,
        calls=found[:size],
        q=q,
        outcome=outcome,
        day=day,
        unhandled=unhandled,
        page_no=page_no,
        has_next=len(found) > size,
    )


@router.get("/{call_pk}")
def call_detail(
    call_pk: int,
    request: Request,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    call = repo.call(db, call_pk)
    if call is None:
        raise HTTPException(404, "no call")
    return page(
        request,
        "calls/detail.html",
        user,
        call=call,
        turns=repo.turns(db, call_pk),
        operators=repo.users(db, only_active=True),
    )


@router.post("/{call_pk}/ticket")
def ticket_from_call(
    call_pk: int,
    request: Request,
    subject: str = Form(""),
    body: str = Form(""),
    category: str = Form(""),
    priority: str = Form("normal"),
    assignee_id: str = Form(""),
    csrf: str = Form(""),
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """One click from "the assistant could not help" to "a person is on it"."""
    require_csrf(request, csrf)
    call = repo.call(db, call_pk)
    if call is None:
        raise HTTPException(404, "no call")
    ticket_id = repo.create_ticket(
        db,
        subject=subject or (call.get("first_question") or "Qoʻngʻiroq"),
        body=body,
        author_id=int(user["id"]),
        contact_id=call["contact_id"],
        call_pk=call_pk,
        category=category,
        priority=priority,
        assignee_id=int(assignee_id) if assignee_id else None,
        sla_hours=settings.sla_hours,
    )
    repo.audit(
        db,
        user_id=int(user["id"]),
        login=user["login"],
        action="ticket_create",
        entity="ticket",
        entity_id=str(ticket_id),
        detail=f"from call {call['call_id']}",
    )
    return redirect(f"/tickets/{ticket_id}")
