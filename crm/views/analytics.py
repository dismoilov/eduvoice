"""Analytics: volume, outcomes, speed, topics and who carries the load."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from crm import repo, stats
from crm.deps import get_db, page, require_roles

router = APIRouter(prefix="/analytics")

# A spreadsheet has to end somewhere; the screen says so when it does.
EXPORT_LIMIT = 5000


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
    """The ministry asks for numbers in a spreadsheet; this is that spreadsheet.

    It covers the same period as the screen it was exported from. It used to ignore
    `days` entirely and hand over the newest 5000 calls whatever was asked for, so the
    totals in the spreadsheet could not be reconciled with the page beside it.
    """
    days = 7 if days not in (7, 14, 30) else days
    since = (date.today() - timedelta(days=days - 1)).isoformat()
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
    for call in repo.calls(db, since=since, limit=EXPORT_LIMIT):
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
