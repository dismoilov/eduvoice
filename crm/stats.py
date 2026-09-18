"""Numbers for the dashboard and the analytics screen.

Every figure here is a query over what actually happened — no estimates, no rounded
"about a second". That matters twice: supervisors act on these numbers, and on the
hackathon stage they are the difference between a claim and a measurement.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta
from typing import Any


def _scalar(db: sqlite3.Connection, sql: str, params: tuple = ()) -> float:
    row = db.execute(sql, params).fetchone()
    value = row[0] if row else 0
    return float(value or 0)


def today() -> str:
    return date.today().strftime("%Y-%m-%d")


def dashboard(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """What a person needs the moment they open the CRM.

    Every figure is one aggregate query: on twenty thousand calls this screen renders in
    milliseconds, because nothing is counted in Python.
    """
    day = today()
    calls = db.execute(
        # 'bot' and not "anything but operator": a caller who hung up in silence was not served
        "SELECT count(*) AS total, sum(outcome = 'bot') AS without_operator"
        " FROM calls WHERE substr(started_at,1,10) = ?",
        (day,),
    ).fetchone()
    answers = db.execute(
        "SELECT count(*) AS questions, avg(nullif(t.total_ms, 0)) AS avg_ms"
        " FROM turns t JOIN calls c ON c.id = t.call_id"
        " WHERE substr(c.started_at,1,10) = ?",
        (day,),
    ).fetchone()
    tickets = db.execute(
        """
        SELECT sum(status IN ('new','in_progress','waiting')) AS open,
               sum(status IN ('new','in_progress','waiting')
                   AND due_at IS NOT NULL AND due_at < datetime('now','localtime')) AS overdue,
               sum(status IN ('new','in_progress','waiting') AND assignee_id = ?) AS mine
        FROM tickets
        """,
        (user_id,),
    ).fetchone()
    total = int(calls["total"] or 0)
    return {
        "calls_today": total,
        "questions_today": int(answers["questions"] or 0),
        "without_operator_percent": (
            round(100 * int(calls["without_operator"] or 0) / total) if total else 0
        ),
        "avg_answer_ms": round(answers["avg_ms"] or 0),
        "open_tickets": int(tickets["open"] or 0),
        "overdue_tickets": int(tickets["overdue"] or 0),
        "my_open_tickets": int(tickets["mine"] or 0),
        "unhandled_calls": int(
            _scalar(
                db,
                "SELECT count(*) FROM calls c WHERE c.outcome = 'operator'"
                " AND NOT EXISTS (SELECT 1 FROM tickets t WHERE t.call_id = c.id)",
            )
        ),
    }


def calls_by_day(db: sqlite3.Connection, days: int = 7) -> list[dict[str, Any]]:
    """Counts per day for the small chart on the dashboard — one grouped query."""
    since = (date.today() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    found = {
        row["day"]: row
        for row in db.execute(
            """
            SELECT substr(started_at,1,10) AS day, count(*) AS calls,
                   sum(outcome = 'bot') AS bot,
                   sum(outcome = 'operator') AS operator,
                   sum(outcome = 'dropped') AS dropped
            FROM calls WHERE substr(started_at,1,10) >= ? GROUP BY day
            """,
            (since,),
        )
    }
    series = []
    for offset in range(days):
        day = (date.today() - timedelta(days=days - 1 - offset)).strftime("%Y-%m-%d")
        row = found.get(day)
        series.append(
            {
                "day": day,
                "label": f"{day[8:10]}.{day[5:7]}",
                "calls": int(row["calls"]) if row else 0,
                "bot": int(row["bot"] or 0) if row else 0,
                "operator": int(row["operator"] or 0) if row else 0,
                "dropped": int(row["dropped"] or 0) if row else 0,
            }
        )
    return series


def analytics(db: sqlite3.Connection, days: int = 14) -> dict[str, Any]:
    """The analytics screen: volume, outcomes, speed, topics, people."""
    since = (date.today() - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    by_day_rows = {
        row["day"]: dict(row)
        for row in db.execute(
            """
            SELECT substr(started_at,1,10) AS day, count(*) AS calls,
                   sum(outcome = 'bot') AS bot,
                   sum(outcome = 'operator') AS operator,
                   sum(outcome = 'dropped') AS dropped
            FROM calls WHERE substr(started_at,1,10) >= ?
            GROUP BY day
            """,
            (since,),
        )
    }
    by_day = []
    for offset in range(days):
        day = (date.today() - timedelta(days=days - 1 - offset)).strftime("%Y-%m-%d")
        row = by_day_rows.get(day, {})
        by_day.append(
            {
                "day": day,
                "label": day[8:10] + "." + day[5:7],
                "calls": int(row.get("calls") or 0),
                "bot": int(row.get("bot") or 0),
                "operator": int(row.get("operator") or 0),
                "dropped": int(row.get("dropped") or 0),
            }
        )

    hours = {
        int(row["hour"]): int(row["calls"])
        for row in db.execute(
            "SELECT substr(started_at,12,2) AS hour, count(*) AS calls FROM calls"
            " WHERE substr(started_at,1,10) >= ? GROUP BY hour",
            (since,),
        )
    }
    by_hour = [{"hour": hour, "calls": hours.get(hour, 0)} for hour in range(24)]

    # One pass over the measurements: three SQL passes (count, median, p90) turned out
    # five times slower than reading the column once and sorting it here.
    ordered = sorted(
        float(row[0])
        for row in db.execute(
            "SELECT t.total_ms FROM turns t JOIN calls c ON c.id = t.call_id"
            " WHERE substr(c.started_at,1,10) >= ? AND t.total_ms > 0",
            (since,),
        )
    )

    return {
        "days": days,
        "since": since,
        "by_day": by_day,
        "by_hour": by_hour,
        "totals": {
            "calls": sum(day["calls"] for day in by_day),
            "bot": sum(day["bot"] for day in by_day),
            "operator": sum(day["operator"] for day in by_day),
            "dropped": sum(day["dropped"] for day in by_day),
        },
        "latency": {
            "average": round(statistics.fmean(ordered)) if ordered else 0,
            "median": round(statistics.median(ordered)) if ordered else 0,
            "p90": round(ordered[int(len(ordered) * 0.9)]) if ordered else 0,
            "answers": len(ordered),
        },
        "topics": [
            dict(row)
            for row in db.execute(
                """
                SELECT CASE WHEN t.faq_id != '' THEN t.faq_id ELSE t.intent END AS topic,
                       count(*) AS count,
                       round(avg(t.total_ms)) AS avg_ms
                FROM turns t JOIN calls c ON c.id = t.call_id
                WHERE substr(c.started_at,1,10) >= ? AND t.question != ''
                GROUP BY topic ORDER BY count DESC LIMIT 10
                """,
                (since,),
            )
        ],
        "questions": [
            dict(row)
            for row in db.execute(
                """
                SELECT max(t.question) AS question, count(*) AS count
                FROM turns t JOIN calls c ON c.id = t.call_id
                WHERE substr(c.started_at,1,10) >= ? AND t.question != ''
                GROUP BY lower(t.question) ORDER BY count DESC LIMIT 10
                """,
                (since,),
            )
        ],
        "operators": [
            dict(row)
            for row in db.execute(
                """
                SELECT u.name,
                       count(t.id) FILTER (WHERE t.status IN ('new','in_progress','waiting'))
                           AS open,
                       count(t.id) FILTER (WHERE t.status IN ('resolved','closed')) AS done,
                       count(t.id) AS total
                FROM users u
                LEFT JOIN tickets t
                       ON t.assignee_id = u.id AND substr(t.created_at, 1, 10) >= ?
                WHERE u.active = 1
                GROUP BY u.id ORDER BY total DESC
                """,
                (since,),
            )
        ],
        "tickets": {
            "open": int(
                _scalar(
                    db,
                    "SELECT count(*) FROM tickets WHERE status IN ('new','in_progress','waiting')",
                )
            ),
            "resolved": int(
                _scalar(db, "SELECT count(*) FROM tickets WHERE status IN ('resolved','closed')")
            ),
            "created": int(
                _scalar(
                    db, "SELECT count(*) FROM tickets WHERE substr(created_at,1,10) >= ?", (since,)
                )
            ),
        },
    }
