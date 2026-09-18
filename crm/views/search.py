"""One search box for three kinds of thing.

The box in the header promises "a question, a number or a ticket", and it used to send
every one of them to the call transcripts — so a ticket number, the most precise thing a
person can type, was the one thing it could never find.
"""

from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends

from crm import repo
from crm.deps import current_user, get_db, redirect

router = APIRouter()

TICKET_NUMBER = re.compile(r"^\d{4}-\d{1,6}$")
PHONE = re.compile(r"^[\d+()\s-]{6,}$")


@router.get("/search")
def search(
    q: str = "",
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Sends the query where it will actually be found."""
    text = q.strip()
    if not text:
        return redirect("/calls")

    if TICKET_NUMBER.match(text):
        found = repo.ticket_by_number(db, text)
        return redirect(f"/tickets/{found['id']}" if found else f"/tickets?q={text}")

    if PHONE.match(text):
        digits = re.sub(r"\D", "", text)
        found = repo.contact_by_phone(db, digits)
        if found:
            return redirect(f"/contacts/{found['id']}")
        return redirect(f"/contacts?q={digits}")

    return redirect(f"/calls?q={text}")
