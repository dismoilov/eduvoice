"""Writing calls into the database — used by the bridge and by the history import.

The bridge writes exactly once per call, at the very end, in one transaction. If the
database is busy or broken, the call is still safe: the JSON line written by
`eduvoice.calllog` remains the backup, and nothing here ever raises into a call.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from store.db import connect, migrate, now, transaction

log = logging.getLogger("store.write")

OUTCOME_BOT = "bot"  # the assistant finished the conversation itself
OUTCOME_OPERATOR = "operator"  # handed over to a person
OUTCOME_DROPPED = "dropped"  # the caller hung up first


def outcome_of(next_action: str, ended_reason: str) -> str:
    """One word for how a call ended — the first thing a supervisor looks at."""
    if ended_reason == "caller_hangup":
        return OUTCOME_DROPPED
    if next_action == "hangup":
        return OUTCOME_BOT
    return OUTCOME_OPERATOR


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def upsert_contact(connection: sqlite3.Connection, phone: str) -> int | None:
    """A citizen is known by the number they call from; the card is created on first call."""
    phone = (phone or "").strip()
    if not phone:
        return None
    row = connection.execute("SELECT id FROM contacts WHERE phone = ?", (phone,)).fetchone()
    if row:
        return int(row["id"])
    stamp = now()
    cursor = connection.execute(
        "INSERT INTO contacts (phone, created_at, updated_at) VALUES (?, ?, ?)",
        (phone, stamp, stamp),
    )
    return int(cursor.lastrowid or 0)


def save_call(connection: sqlite3.Connection, entry: dict[str, Any]) -> int:
    """Stores one finished call with its turns. Re-saving the same call replaces it."""
    turns = entry.get("turns") or []
    answered = [_float((t.get("latency_ms") or {}).get("total_ms")) for t in turns]
    answered = [value for value in answered if value]
    with transaction(connection):
        contact_id = upsert_contact(connection, entry.get("caller", ""))
        connection.execute("DELETE FROM calls WHERE call_id = ?", (entry["call_id"],))
        cursor = connection.execute(
            """
            INSERT INTO calls (call_id, contact_id, caller, direction, started_at, duration_s,
                               outcome, ended_reason, next_action, recording, questions,
                               avg_answer_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["call_id"],
                contact_id,
                entry.get("caller", ""),
                entry.get("direction", "in"),
                entry.get("started_at") or now(),
                _float(entry.get("duration_s")),
                outcome_of(entry.get("next_action", ""), entry.get("ended_reason", "")),
                entry.get("ended_reason", ""),
                entry.get("next_action", ""),
                entry.get("recording", ""),
                len(turns),
                round(sum(answered) / len(answered), 1) if answered else 0.0,
            ),
        )
        call_row_id = int(cursor.lastrowid or 0)
        for position, turn in enumerate(turns, start=1):
            latency = turn.get("latency_ms") or {}
            connection.execute(
                """
                INSERT INTO turns (call_id, position, question, answer, intent, action, faq_id,
                                   source, at_ms, stt_ms, decision_ms, total_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_row_id,
                    position,
                    turn.get("question", ""),
                    turn.get("answer", ""),
                    turn.get("intent", ""),
                    turn.get("action", ""),
                    turn.get("faq_id", ""),
                    turn.get("source", ""),
                    _float(turn.get("at_ms")),
                    _float(latency.get("stt_ms")),
                    _float(latency.get("decision_ms")),
                    _float(latency.get("total_ms")),
                ),
            )
    return call_row_id


class CallStore:
    """The bridge's door to the database: one call in, never an exception out."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def save(self, entry: dict[str, Any]) -> None:
        try:
            connection = connect(self._path)
            try:
                migrate(connection)
                save_call(connection, entry)
            finally:
                connection.close()
        except Exception as exc:  # a database problem must never break a call
            log.warning("could not store call %s: %s", entry.get("call_id", "?"), exc)


def import_jsonl(logs_dir: Path, connection: sqlite3.Connection) -> int:
    """Loads the history written before the database existed. Safe to run twice."""
    imported = 0
    for path in sorted(Path(logs_dir).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("call_id"):
                    save_call(connection, entry)
                    imported += 1
            except (ValueError, KeyError, sqlite3.Error) as exc:
                log.warning("skipping a damaged line in %s: %s", path.name, exc)
    return imported
