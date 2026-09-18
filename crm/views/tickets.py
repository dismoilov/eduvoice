"""Requests from citizens: the queue, one request and everything that happened to it."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from crm import repo
from crm.config import settings
from crm.deps import current_user, get_db, page, redirect
from crm.views.common import as_id, require_csrf

router = APIRouter(prefix="/tickets")


@router.get("")
def ticket_list(
    request: Request,
    status: str = "open",
    mine: str = "",
    unassigned: str = "",
    overdue: str = "",
    q: str = "",
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    found = repo.tickets(
        db,
        status=status,
        assignee_id=int(user["id"]) if mine else None,
        unassigned=bool(unassigned),
        overdue=bool(overdue),
        query=q,
        limit=200,
    )
    return page(
        request,
        "tickets/list.html",
        user,
        tickets=found,
        status=status,
        mine=mine,
        unassigned=unassigned,
        overdue=overdue,
        q=q,
        operators=repo.users(db, only_active=True),
    )


@router.get("/new")
def ticket_new(
    request: Request,
    contact_id: int | None = None,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Opened from a citizen card, the form already knows whose request this is."""
    contact = repo.contact(db, contact_id) if contact_id else None
    return page(
        request,
        "tickets/new.html",
        user,
        operators=repo.users(db, only_active=True),
        contacts=repo.contacts(db, limit=50),
        phone=contact["phone"] if contact else "",
    )


@router.post("/new")
def ticket_create(
    request: Request,
    subject: str = Form(""),
    body: str = Form(""),
    category: str = Form(""),
    priority: str = Form("normal"),
    phone: str = Form(""),
    assignee_id: str = Form(""),
    csrf: str = Form(""),
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf)
    if not subject.strip():
        return redirect("/tickets/new")
    contact_id = repo.ensure_contact(db, phone) if phone.strip() else None
    ticket_id = repo.create_ticket(
        db,
        subject=subject,
        body=body,
        author_id=int(user["id"]),
        contact_id=contact_id,
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
    )
    return redirect(f"/tickets/{ticket_id}")


@router.get("/{ticket_id}")
def ticket_detail(
    ticket_id: int,
    request: Request,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    ticket = repo.ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(404, "no ticket")
    linked_calls = []
    if ticket["contact_id"]:
        linked_calls = repo.calls(db, contact_id=ticket["contact_id"], limit=10)
    return page(
        request,
        "tickets/detail.html",
        user,
        ticket=ticket,
        events=repo.ticket_events(db, ticket_id),
        operators=repo.users(db, only_active=True),
        calls=linked_calls,
        statuses=repo.STATUSES,
        priorities=repo.PRIORITIES,
    )


@router.post("/{ticket_id}")
def ticket_update(
    ticket_id: int,
    request: Request,
    status: str = Form(""),
    priority: str = Form(""),
    assignee_id: str = Form(""),
    category: str = Form(""),
    resolution: str = Form(""),
    csrf: str = Form(""),
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf)
    fields: dict = {}
    if status in repo.STATUSES:
        fields["status"] = status
    if priority in repo.PRIORITIES:
        fields["priority"] = priority
    if assignee_id != "":
        fields["assignee_id"] = as_id(assignee_id)
    if category:
        fields["category"] = category
    if resolution:
        fields["resolution"] = resolution
    repo.update_ticket(db, ticket_id, int(user["id"]), **fields)
    repo.audit(
        db,
        user_id=int(user["id"]),
        login=user["login"],
        action="ticket_update",
        entity="ticket",
        entity_id=str(ticket_id),
        detail=str(fields),
    )
    return redirect(f"/tickets/{ticket_id}")


@router.post("/{ticket_id}/comment")
def ticket_comment(
    ticket_id: int,
    request: Request,
    text: str = Form(""),
    csrf: str = Form(""),
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf)
    repo.add_comment(db, ticket_id, int(user["id"]), text)
    return redirect(f"/tickets/{ticket_id}")
