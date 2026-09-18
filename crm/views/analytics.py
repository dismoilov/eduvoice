"""Analytics: volume, outcomes, speed, topics and who carries the load."""

from __future__ import annotations

import csv
import io
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from crm import repo, stats
from crm.deps import get_db, page, require_roles

router = APIRouter(prefix="/analytics")


@router.get("")
def analytics(
    request: Request,
    days: int = 14,
    user: dict = Depends(require_roles("supervisor", "admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    days = 7 if days not in (7, 14, 30) else days
    return page(request, "analytics.html", user, data=stats.analytics(db, days), days=days)


@router.get("/export.csv")
def export_calls(
    days: int = 30,
    user: dict = Depends(require_roles("supervisor", "admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    """The ministry asks for numbers in a spreadsheet; this is that spreadsheet."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            "started_at",
            "phone",
            "duration_s",
            "outcome",
            "questions",
            "avg_answer_ms",
            "first_question",
        ]
    )
    for call in repo.calls(db, limit=5000):
        writer.writerow(
            [
                call["started_at"],
                call["caller"],
                call["duration_s"],
                call["outcome"],
                call["questions"],
                call["avg_answer_ms"],
                (call.get("first_question") or ""),
            ]
        )
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="eduvoice-calls.csv"'},
    )
