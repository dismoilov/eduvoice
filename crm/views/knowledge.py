"""The knowledge base — the screen that changes what the assistant says.

A supervisor edits an answer here, presses publish, and the next caller hears the new
wording. Drafts are invisible to the bridge, and every save keeps the previous version:
these answers quote regulations, so "who changed this and when" has to be answerable.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from crm import repo
from crm.deps import current_user, get_db, page, redirect, require_roles
from crm.views.common import require_csrf

router = APIRouter(prefix="/knowledge")


@router.get("")
def knowledge_list(
    request: Request,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    entries = repo.knowledge_entries(db)
    return page(
        request,
        "knowledge/list.html",
        user,
        entries=entries,
        published=sum(1 for entry in entries if entry["status"] == "published"),
    )


@router.get("/new")
def knowledge_new(
    request: Request,
    user: dict = Depends(require_roles("supervisor", "admin")),
):
    return page(
        request,
        "knowledge/edit.html",
        user,
        entry=None,
        versions=[],
        problem=request.query_params.get("problem", ""),
    )


@router.post("/new")
def knowledge_create(
    request: Request,
    faq_id: str = Form(""),
    question: str = Form(""),
    answer: str = Form(""),
    keywords: str = Form(""),
    source: str = Form(""),
    csrf: str = Form(""),
    user: dict = Depends(require_roles("supervisor", "admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf)
    if not faq_id.strip() or not answer.strip():
        return redirect("/knowledge/new?problem=incomplete")
    if repo.knowledge_by_faq_id(db, faq_id):
        # `faq_id` is the key the assistant looks answers up by: it must stay unique.
        return redirect("/knowledge/new?problem=duplicate")
    entry_id = repo.create_knowledge(
        db,
        faq_id=faq_id,
        question=question,
        answer=answer,
        keywords=keywords,
        source=source,
        author_id=int(user["id"]),
    )
    repo.audit(
        db,
        user_id=int(user["id"]),
        login=user["login"],
        action="knowledge_create",
        entity="knowledge",
        entity_id=str(entry_id),
        detail=faq_id,
    )
    return redirect(f"/knowledge/{entry_id}")


@router.get("/{entry_id}")
def knowledge_edit(
    entry_id: int,
    request: Request,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    entry = repo.knowledge_entry(db, entry_id)
    if entry is None:
        raise HTTPException(404, "no entry")
    return page(
        request,
        "knowledge/edit.html",
        user,
        entry=entry,
        versions=repo.knowledge_versions(db, entry_id),
    )


@router.post("/{entry_id}")
def knowledge_save(
    entry_id: int,
    request: Request,
    question: str = Form(""),
    answer: str = Form(""),
    keywords: str = Form(""),
    source: str = Form(""),
    csrf: str = Form(""),
    user: dict = Depends(require_roles("supervisor", "admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf)
    if not answer.strip():
        # An empty answer is not an answer: the assistant would play silence down the
        # line, and the caller would sit through it twice before being hung up on.
        return redirect(f"/knowledge/{entry_id}?problem=empty_answer")
    repo.update_knowledge(
        db,
        entry_id,
        int(user["id"]),
        question=question,
        answer=answer,
        keywords=keywords,
        source=source,
    )
    repo.audit(
        db,
        user_id=int(user["id"]),
        login=user["login"],
        action="knowledge_update",
        entity="knowledge",
        entity_id=str(entry_id),
    )
    return redirect(f"/knowledge/{entry_id}")


@router.post("/{entry_id}/status")
def knowledge_status(
    entry_id: int,
    request: Request,
    status: str = Form("draft"),
    csrf: str = Form(""),
    user: dict = Depends(require_roles("supervisor", "admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    """Publishing is a separate, deliberate act: it changes what citizens hear."""
    require_csrf(request, csrf)
    if status not in ("draft", "published"):
        raise HTTPException(400, "bad status")
    repo.update_knowledge(db, entry_id, int(user["id"]), status=status)
    repo.audit(
        db,
        user_id=int(user["id"]),
        login=user["login"],
        action=f"knowledge_{status}",
        entity="knowledge",
        entity_id=str(entry_id),
    )
    return redirect(f"/knowledge/{entry_id}")
