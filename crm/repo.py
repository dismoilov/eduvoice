"""Every database query the CRM makes.

Kept in one place on purpose: the routes stay readable, and anybody reviewing what the
system does with citizens' data can read this single file instead of hunting through
templates. Plain SQL, no ORM — the schema is small and the queries are the interesting
part.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from store.db import now, transaction

OPEN_STATUSES = ("new", "in_progress", "waiting")
STATUSES = ("new", "in_progress", "waiting", "resolved", "closed")
PRIORITIES = ("low", "normal", "high")
ROLES = ("operator", "supervisor", "admin")


def rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def one(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    return dict(row) if row else None


# ============================================================================ users


def user_by_login(db: sqlite3.Connection, login: str) -> dict[str, Any] | None:
    return one(db.execute("SELECT * FROM users WHERE login = ? AND active = 1", (login.strip(),)))


def user(db: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    return one(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)))


def users(db: sqlite3.Connection, only_active: bool = False) -> list[dict[str, Any]]:
    where = "WHERE active = 1" if only_active else ""
    return rows(db.execute(f"SELECT * FROM users {where} ORDER BY active DESC, name"))


def create_user(
    db: sqlite3.Connection,
    login: str,
    name: str,
    role: str,
    password_hash: str,
    extension: str = "",
) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO users (login, name, role, extension, password_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (login.strip(), name.strip(), role, extension.strip(), password_hash, now()),
        )
    return int(cursor.lastrowid or 0)


def update_user(db: sqlite3.Connection, user_id: int, **fields: Any) -> None:
    allowed = {"name", "role", "extension", "active", "password_hash"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if not changes:
        return
    assignments = ", ".join(f"{key} = ?" for key in changes)
    with transaction(db):
        db.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*changes.values(), user_id))


# ========================================================================= contacts


def contact(db: sqlite3.Connection, contact_id: int) -> dict[str, Any] | None:
    return one(db.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)))


def contact_by_phone(db: sqlite3.Connection, phone: str) -> dict[str, Any] | None:
    return one(db.execute("SELECT * FROM contacts WHERE phone = ?", (phone.strip(),)))


def contacts(db: sqlite3.Connection, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    """Citizens with how often they called and how many requests they have."""
    like = f"%{query.strip()}%"
    return rows(
        db.execute(
            """
            SELECT c.*,
                   (SELECT count(*) FROM calls WHERE contact_id = c.id) AS calls,
                   (SELECT max(started_at) FROM calls WHERE contact_id = c.id) AS last_call,
                   (SELECT count(*) FROM tickets WHERE contact_id = c.id) AS tickets
            FROM contacts c
            WHERE (? = '' OR c.phone LIKE ? OR c.name LIKE ?)
            ORDER BY last_call DESC NULLS LAST, c.id DESC
            LIMIT ?
            """,
            (query.strip(), like, like, limit),
        )
    )


def ensure_contact(db: sqlite3.Connection, phone: str) -> int:
    existing = contact_by_phone(db, phone)
    if existing:
        return int(existing["id"])
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO contacts (phone, created_at, updated_at) VALUES (?, ?, ?)",
            (phone.strip(), now(), now()),
        )
    return int(cursor.lastrowid or 0)


def update_contact(db: sqlite3.Connection, contact_id: int, **fields: Any) -> None:
    allowed = {"name", "kind", "university", "note"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if not changes:
        return
    assignments = ", ".join(f"{key} = ?" for key in changes)
    with transaction(db):
        db.execute(
            f"UPDATE contacts SET {assignments}, updated_at = ? WHERE id = ?",
            (*changes.values(), now(), contact_id),
        )


# ============================================================================ calls


CALL_COLUMNS = """
    c.*, ct.phone AS contact_phone, ct.name AS contact_name,
    (SELECT count(*) FROM tickets t WHERE t.call_id = c.id) AS ticket_count,
    (SELECT t.id FROM tickets t WHERE t.call_id = c.id LIMIT 1) AS ticket_id,
    (SELECT t.number FROM tickets t WHERE t.call_id = c.id LIMIT 1) AS ticket_number
"""


def calls(
    db: sqlite3.Connection,
    *,
    query: str = "",
    outcome: str = "",
    day: str = "",
    contact_id: int | None = None,
    unhandled: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Calls with the filters the list screen offers. `query` searches the transcript."""
    where = ["1 = 1"]
    params: list[Any] = []
    if outcome:
        where.append("c.outcome = ?")
        params.append(outcome)
    if day:
        where.append("substr(c.started_at, 1, 10) = ?")
        params.append(day)
    if contact_id:
        where.append("c.contact_id = ?")
        params.append(contact_id)
    if unhandled:
        where.append(
            "c.outcome = 'operator' AND NOT EXISTS (SELECT 1 FROM tickets t WHERE t.call_id = c.id)"
        )
    if query.strip():
        # Full text over what was said, plus the plain number.
        where.append(
            "(c.caller LIKE ? OR c.call_id LIKE ? OR c.id IN"
            " (SELECT t.call_id FROM turns t JOIN turns_fts f ON f.rowid = t.id"
            "  WHERE turns_fts MATCH ?))"
        )
        like = f"%{query.strip()}%"
        params += [like, like, _fts_query(query)]
    params += [limit, offset]
    return rows(
        db.execute(
            f"""
            SELECT {CALL_COLUMNS},
                   (SELECT question FROM turns WHERE call_id = c.id ORDER BY position LIMIT 1)
                       AS first_question
            FROM calls c LEFT JOIN contacts ct ON ct.id = c.contact_id
            WHERE {" AND ".join(where)}
            ORDER BY c.started_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        )
    )


def _fts_query(text: str) -> str:
    """Turns free text into an FTS5 prefix query, quoting each word."""
    words = [word for word in text.replace('"', " ").split() if word]
    return " ".join(f'"{word}"*' for word in words) or '""'


def call(db: sqlite3.Connection, call_pk: int) -> dict[str, Any] | None:
    return one(
        db.execute(
            f"SELECT {CALL_COLUMNS} FROM calls c LEFT JOIN contacts ct ON ct.id = c.contact_id"
            " WHERE c.id = ?",
            (call_pk,),
        )
    )


def call_by_uuid(db: sqlite3.Connection, call_id: str) -> dict[str, Any] | None:
    return one(db.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)))


def turns(db: sqlite3.Connection, call_pk: int) -> list[dict[str, Any]]:
    return rows(db.execute("SELECT * FROM turns WHERE call_id = ? ORDER BY position", (call_pk,)))


# ========================================================================== tickets


TICKET_COLUMNS = """
    t.*, ct.phone AS contact_phone, ct.name AS contact_name,
    u.name AS assignee_name, a.name AS author_name,
    c.call_id AS call_uuid, c.started_at AS call_started_at
"""
TICKET_JOINS = """
    FROM tickets t
    LEFT JOIN contacts ct ON ct.id = t.contact_id
    LEFT JOIN users u ON u.id = t.assignee_id
    LEFT JOIN users a ON a.id = t.author_id
    LEFT JOIN calls c ON c.id = t.call_id
"""


def tickets(
    db: sqlite3.Connection,
    *,
    status: str = "",
    assignee_id: int | None = None,
    unassigned: bool = False,
    overdue: bool = False,
    query: str = "",
    contact_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where = ["1 = 1"]
    params: list[Any] = []
    if status == "open":
        where.append(f"t.status IN ({','.join('?' * len(OPEN_STATUSES))})")
        params += list(OPEN_STATUSES)
    elif status:
        where.append("t.status = ?")
        params.append(status)
    if assignee_id:
        where.append("t.assignee_id = ?")
        params.append(assignee_id)
    if unassigned:
        where.append("t.assignee_id IS NULL")
    if overdue:
        where.append(
            "t.due_at IS NOT NULL AND t.due_at < ? AND t.status IN ('new','in_progress','waiting')"
        )
        params.append(now())
    if contact_id:
        where.append("t.contact_id = ?")
        params.append(contact_id)
    if query.strip():
        like = f"%{query.strip()}%"
        where.append("(t.subject LIKE ? OR t.body LIKE ? OR t.number LIKE ? OR ct.phone LIKE ?)")
        params += [like, like, like, like]
    params.append(limit)
    return rows(
        db.execute(
            f"SELECT {TICKET_COLUMNS} {TICKET_JOINS} WHERE {' AND '.join(where)}"
            " ORDER BY CASE t.status WHEN 'new' THEN 0 WHEN 'in_progress' THEN 1"
            " WHEN 'waiting' THEN 2 ELSE 3 END, t.due_at IS NULL, t.due_at, t.id DESC LIMIT ?",
            params,
        )
    )


def ticket(db: sqlite3.Connection, ticket_id: int) -> dict[str, Any] | None:
    return one(db.execute(f"SELECT {TICKET_COLUMNS} {TICKET_JOINS} WHERE t.id = ?", (ticket_id,)))


def next_ticket_number(db: sqlite3.Connection) -> str:
    year = datetime.now().year
    row = db.execute(
        "SELECT count(*) AS n FROM tickets WHERE number LIKE ?", (f"{year}-%",)
    ).fetchone()
    return f"{year}-{int(row['n']) + 1:04d}"


def create_ticket(
    db: sqlite3.Connection,
    *,
    subject: str,
    body: str,
    author_id: int,
    contact_id: int | None = None,
    call_pk: int | None = None,
    category: str = "",
    priority: str = "normal",
    assignee_id: int | None = None,
    sla_hours: int = 24,
) -> int:
    stamp = now()
    due = (datetime.now() + timedelta(hours=sla_hours)).strftime("%Y-%m-%d %H:%M:%S")
    with transaction(db):
        cursor = db.execute(
            """
            INSERT INTO tickets (number, subject, body, category, status, priority, contact_id,
                                 assignee_id, author_id, call_id, due_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_ticket_number(db),
                subject.strip(),
                body.strip(),
                category.strip(),
                priority if priority in PRIORITIES else "normal",
                contact_id,
                assignee_id,
                author_id,
                call_pk,
                due,
                stamp,
                stamp,
            ),
        )
        ticket_id = int(cursor.lastrowid or 0)
        db.execute(
            "INSERT INTO ticket_events (ticket_id, author_id, kind, text, created_at)"
            " VALUES (?, ?, 'created', '', ?)",
            (ticket_id, author_id, stamp),
        )
    return ticket_id


def update_ticket(db: sqlite3.Connection, ticket_id: int, author_id: int, **fields: Any) -> None:
    """Applies changes and writes the timeline entries a reader expects to see."""
    current = ticket(db, ticket_id)
    if current is None:
        return
    allowed = {
        "status",
        "priority",
        "assignee_id",
        "category",
        "subject",
        "body",
        "resolution",
        "due_at",
    }
    changes = {k: v for k, v in fields.items() if k in allowed and v != current.get(k)}
    if not changes:
        return
    stamp = now()
    with transaction(db):
        if changes.get("status") in ("resolved", "closed"):
            changes["resolved_at"] = stamp
        assignments = ", ".join(f"{key} = ?" for key in changes)
        db.execute(
            f"UPDATE tickets SET {assignments}, updated_at = ? WHERE id = ?",
            (*changes.values(), stamp, ticket_id),
        )
        if "status" in changes:
            db.execute(
                "INSERT INTO ticket_events (ticket_id, author_id, kind, text, created_at)"
                " VALUES (?, ?, 'status', ?, ?)",
                (ticket_id, author_id, changes["status"], stamp),
            )
        if "assignee_id" in changes:
            assignee = user(db, int(changes["assignee_id"])) if changes["assignee_id"] else None
            db.execute(
                "INSERT INTO ticket_events (ticket_id, author_id, kind, text, created_at)"
                " VALUES (?, ?, 'assign', ?, ?)",
                (ticket_id, author_id, assignee["name"] if assignee else "", stamp),
            )


def add_comment(db: sqlite3.Connection, ticket_id: int, author_id: int, text: str) -> None:
    if not text.strip():
        return
    with transaction(db):
        db.execute(
            "INSERT INTO ticket_events (ticket_id, author_id, kind, text, created_at)"
            " VALUES (?, ?, 'comment', ?, ?)",
            (ticket_id, author_id, text.strip(), now()),
        )
        db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now(), ticket_id))


def ticket_events(db: sqlite3.Connection, ticket_id: int) -> list[dict[str, Any]]:
    return rows(
        db.execute(
            "SELECT e.*, u.name AS author_name FROM ticket_events e"
            " LEFT JOIN users u ON u.id = e.author_id"
            " WHERE e.ticket_id = ? ORDER BY e.id",
            (ticket_id,),
        )
    )


# ======================================================================== knowledge


def knowledge_entries(db: sqlite3.Connection, status: str = "") -> list[dict[str, Any]]:
    """Answers with how often the assistant used each one in the last 30 days."""
    where = "WHERE k.status = ?" if status else ""
    params: list[Any] = [status] if status else []
    return rows(
        db.execute(
            f"""
            SELECT k.*, u.name AS author_name,
                   (SELECT count(*) FROM turns t JOIN calls c ON c.id = t.call_id
                     WHERE t.faq_id = k.faq_id AND c.started_at >= date('now', '-30 day'))
                   AS used_30d
            FROM knowledge k LEFT JOIN users u ON u.id = k.author_id
            {where}
            ORDER BY k.status = 'draft' DESC, k.faq_id
            """,
            params,
        )
    )


def knowledge_entry(db: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
    return one(db.execute("SELECT * FROM knowledge WHERE id = ?", (entry_id,)))


def create_knowledge(
    db: sqlite3.Connection,
    *,
    faq_id: str,
    question: str,
    answer: str,
    keywords: str,
    source: str,
    author_id: int,
) -> int:
    stamp = now()
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO knowledge (faq_id, question, answer, keywords, source, status, version,"
            " author_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'draft', 1, ?, ?, ?)",
            (
                faq_id.strip(),
                question.strip(),
                answer.strip(),
                keywords.strip(),
                source.strip(),
                author_id,
                stamp,
                stamp,
            ),
        )
    return int(cursor.lastrowid or 0)


def update_knowledge(db: sqlite3.Connection, entry_id: int, author_id: int, **fields: Any) -> None:
    """Saves a new version and keeps the old one: an answer is a regulation, not a note."""
    current = knowledge_entry(db, entry_id)
    if current is None:
        return
    allowed = {"question", "answer", "keywords", "source", "status"}
    changes = {k: v for k, v in fields.items() if k in allowed and v != current.get(k)}
    if not changes:
        return
    stamp = now()
    with transaction(db):
        db.execute(
            "INSERT INTO knowledge_versions (knowledge_id, version, answer, keywords, source,"
            " status, author_id, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                current["version"],
                current["answer"],
                current["keywords"],
                current["source"],
                current["status"],
                author_id,
                stamp,
            ),
        )
        assignments = ", ".join(f"{key} = ?" for key in changes)
        db.execute(
            f"UPDATE knowledge SET {assignments}, version = version + 1, author_id = ?,"
            " updated_at = ? WHERE id = ?",
            (*changes.values(), author_id, stamp, entry_id),
        )


def knowledge_versions(db: sqlite3.Connection, entry_id: int) -> list[dict[str, Any]]:
    return rows(
        db.execute(
            "SELECT v.*, u.name AS author_name FROM knowledge_versions v"
            " LEFT JOIN users u ON u.id = v.author_id"
            " WHERE knowledge_id = ? ORDER BY v.id DESC",
            (entry_id,),
        )
    )


# ======================================================================== callbacks


def create_callback(
    db: sqlite3.Connection,
    *,
    contact_id: int,
    due_at: str,
    assignee_id: int | None,
    ticket_id: int | None = None,
) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO callbacks (contact_id, ticket_id, assignee_id, due_at, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (contact_id, ticket_id, assignee_id, due_at, now()),
        )
    return int(cursor.lastrowid or 0)


def callbacks(db: sqlite3.Connection, *, assignee_id: int | None = None, pending: bool = True):
    where = ["1 = 1"]
    params: list[Any] = []
    if pending:
        where.append("b.status = 'planned'")
    if assignee_id:
        where.append("b.assignee_id = ?")
        params.append(assignee_id)
    return rows(
        db.execute(
            f"""
            SELECT b.*, c.phone, c.name AS contact_name, u.name AS assignee_name
            FROM callbacks b
            LEFT JOIN contacts c ON c.id = b.contact_id
            LEFT JOIN users u ON u.id = b.assignee_id
            WHERE {" AND ".join(where)} ORDER BY b.due_at
            """,
            params,
        )
    )


def complete_callback(db: sqlite3.Connection, callback_id: int, result: str) -> None:
    with transaction(db):
        db.execute(
            "UPDATE callbacks SET status = 'done', result = ? WHERE id = ?",
            (result.strip(), callback_id),
        )


# ============================================================================ audit


def audit(
    db: sqlite3.Connection,
    *,
    user_id: int | None,
    login: str,
    action: str,
    entity: str = "",
    entity_id: str = "",
    detail: str = "",
) -> None:
    with transaction(db):
        db.execute(
            "INSERT INTO audit (at, user_id, login, action, entity, entity_id, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now(), user_id, login, action, entity, str(entity_id), detail),
        )


def audit_log(db: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    return rows(db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)))
