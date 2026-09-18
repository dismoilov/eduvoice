"""The database shared by the voice bridge and the CRM.

SQLite on purpose: the whole system lives on one machine, the load is a few calls per
minute, and a file database needs no second server to keep alive during a demo. It is a
real database all the same — transactions, foreign keys, full-text search.

Two processes write here: the bridge (one row per finished call) and the CRM (everything
an operator does). That works because of three settings applied to every connection:

    journal_mode = WAL    readers never block the writer, and the writer never blocks readers
    busy_timeout = 5000   a locked database is waited for, not an error
    foreign_keys = ON     a ticket cannot point at a call that does not exist

The schema is versioned with `PRAGMA user_version`: every migration below runs once, in
order, and running them again is a no-op.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_PATH = Path(os.getenv("EDUVOICE_DB", "data/eduvoice.db"))

# --------------------------------------------------------------------------- schema

MIGRATIONS: list[str] = [
    # 1 — people who use the CRM
    """
    CREATE TABLE users (
        id            INTEGER PRIMARY KEY,
        login         TEXT NOT NULL UNIQUE,
        name          TEXT NOT NULL,
        role          TEXT NOT NULL CHECK (role IN ('operator', 'supervisor', 'admin')),
        extension     TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL,
        active        INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL
    );

    -- Citizens, keyed by the number they call from.
    CREATE TABLE contacts (
        id         INTEGER PRIMARY KEY,
        phone      TEXT NOT NULL UNIQUE,
        name       TEXT NOT NULL DEFAULT '',
        kind       TEXT NOT NULL DEFAULT '',
        university TEXT NOT NULL DEFAULT '',
        note       TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE calls (
        id            INTEGER PRIMARY KEY,
        call_id       TEXT NOT NULL UNIQUE,
        contact_id    INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
        caller        TEXT NOT NULL DEFAULT '',
        direction     TEXT NOT NULL DEFAULT 'in',
        started_at    TEXT NOT NULL,
        duration_s    REAL NOT NULL DEFAULT 0,
        outcome       TEXT NOT NULL DEFAULT 'dropped',
        ended_reason  TEXT NOT NULL DEFAULT '',
        next_action   TEXT NOT NULL DEFAULT '',
        recording     TEXT NOT NULL DEFAULT '',
        questions     INTEGER NOT NULL DEFAULT 0,
        avg_answer_ms REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX calls_started ON calls(started_at DESC);
    CREATE INDEX calls_contact ON calls(contact_id);
    CREATE INDEX calls_outcome ON calls(outcome);

    -- One question and what the assistant did about it.
    CREATE TABLE turns (
        id          INTEGER PRIMARY KEY,
        call_id     INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
        position    INTEGER NOT NULL,
        question    TEXT NOT NULL DEFAULT '',
        answer      TEXT NOT NULL DEFAULT '',
        intent      TEXT NOT NULL DEFAULT '',
        action      TEXT NOT NULL DEFAULT '',
        faq_id      TEXT NOT NULL DEFAULT '',
        source      TEXT NOT NULL DEFAULT '',
        at_ms       REAL NOT NULL DEFAULT 0,
        stt_ms      REAL NOT NULL DEFAULT 0,
        decision_ms REAL NOT NULL DEFAULT 0,
        total_ms    REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX turns_call ON turns(call_id);
    CREATE INDEX turns_intent ON turns(intent);

    CREATE TABLE tickets (
        id          INTEGER PRIMARY KEY,
        number      TEXT NOT NULL UNIQUE,
        subject     TEXT NOT NULL,
        body        TEXT NOT NULL DEFAULT '',
        category    TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'in_progress', 'waiting', 'resolved', 'closed')),
        priority    TEXT NOT NULL DEFAULT 'normal'
                    CHECK (priority IN ('low', 'normal', 'high')),
        contact_id  INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
        assignee_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        author_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
        call_id     INTEGER REFERENCES calls(id) ON DELETE SET NULL,
        due_at      TEXT,
        resolution  TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        resolved_at TEXT
    );
    CREATE INDEX tickets_status ON tickets(status);
    CREATE INDEX tickets_assignee ON tickets(assignee_id);
    CREATE INDEX tickets_due ON tickets(due_at);

    -- Comments and status changes share one timeline: that is what people read.
    CREATE TABLE ticket_events (
        id         INTEGER PRIMARY KEY,
        ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        author_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
        kind       TEXT NOT NULL,
        text       TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE INDEX ticket_events_ticket ON ticket_events(ticket_id);

    -- What the assistant is allowed to say. Only `published` rows reach the caller.
    CREATE TABLE knowledge (
        id         INTEGER PRIMARY KEY,
        faq_id     TEXT NOT NULL UNIQUE,
        question   TEXT NOT NULL DEFAULT '',
        answer     TEXT NOT NULL,
        keywords   TEXT NOT NULL DEFAULT '',
        source     TEXT NOT NULL DEFAULT '',
        status     TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
        version    INTEGER NOT NULL DEFAULT 1,
        author_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX knowledge_status ON knowledge(status);

    CREATE TABLE knowledge_versions (
        id           INTEGER PRIMARY KEY,
        knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
        version      INTEGER NOT NULL,
        answer       TEXT NOT NULL,
        keywords     TEXT NOT NULL DEFAULT '',
        source       TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL,
        author_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
        saved_at     TEXT NOT NULL
    );

    CREATE TABLE callbacks (
        id          INTEGER PRIMARY KEY,
        contact_id  INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
        ticket_id   INTEGER REFERENCES tickets(id) ON DELETE SET NULL,
        assignee_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        due_at      TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'planned'
                    CHECK (status IN ('planned', 'done', 'cancelled')),
        result      TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    );
    CREATE INDEX callbacks_due ON callbacks(due_at);

    -- Who changed what: personal data is involved, so every change leaves a trace.
    CREATE TABLE audit (
        id        INTEGER PRIMARY KEY,
        at        TEXT NOT NULL,
        user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
        login     TEXT NOT NULL DEFAULT '',
        action    TEXT NOT NULL,
        entity    TEXT NOT NULL DEFAULT '',
        entity_id TEXT NOT NULL DEFAULT '',
        detail    TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX audit_at ON audit(at DESC);

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2 — full-text search over what was said, so a supervisor can find "stipendiya"
    #     in a thousand calls. FTS5 is compiled into the SQLite we ship with.
    """
    CREATE VIRTUAL TABLE turns_fts USING fts5(
        question, answer, content='turns', content_rowid='id', tokenize='unicode61'
    );
    CREATE TRIGGER turns_fts_insert AFTER INSERT ON turns BEGIN
        INSERT INTO turns_fts(rowid, question, answer) VALUES (new.id, new.question, new.answer);
    END;
    CREATE TRIGGER turns_fts_delete AFTER DELETE ON turns BEGIN
        INSERT INTO turns_fts(turns_fts, rowid, question, answer)
        VALUES ('delete', old.id, old.question, old.answer);
    END;
    CREATE TRIGGER turns_fts_update AFTER UPDATE ON turns BEGIN
        INSERT INTO turns_fts(turns_fts, rowid, question, answer)
        VALUES ('delete', old.id, old.question, old.answer);
        INSERT INTO turns_fts(rowid, question, answer) VALUES (new.id, new.question, new.answer);
    END;
    """,
    # 3 — the indexes the screens actually need. Measured on 20 000 calls: without them
    #     the dashboard took 0.9 s and the filtered call list 0.4 s, because every row ran
    #     a correlated subquery over an unindexed tickets table.
    """
    CREATE INDEX tickets_call ON tickets(call_id);
    CREATE INDEX tickets_contact ON tickets(contact_id);
    CREATE INDEX calls_day ON calls(substr(started_at, 1, 10));
    CREATE INDEX turns_call_position ON turns(call_id, position);
    CREATE INDEX ticket_events_order ON ticket_events(ticket_id, id);
    CREATE INDEX callbacks_assignee ON callbacks(assignee_id, status);
    """,
    # 4 — calls recorded before the outcome rule was corrected. A caller who heard the
    #     whole answer and then hung up was filed as "dropped", which both hid the
    #     assistant's work and let a silent hang-up count towards "handled without an
    #     operator". Only calls that actually delivered an answer are moved.
    """
    UPDATE calls SET outcome = 'bot'
     WHERE outcome = 'dropped' AND ended_reason = 'caller_hangup' AND questions > 0;
    """,
    # 5 — migration 4 counted any turn as an answer, which is wrong: a turn is also
    #     written when the assistant says "I did not understand" or "I am transferring
    #     you". Recomputed here from what each turn actually did, by the same rule the
    #     bridge now uses, so a call means the same thing whichever path recorded it.
    """
    UPDATE calls SET outcome = CASE
        WHEN ended_reason IN ('transfer', 'tech_problem') THEN 'operator'
        WHEN next_action = 'operator' AND ended_reason != 'caller_hangup' THEN 'operator'
        WHEN (SELECT count(*) FROM turns t
               WHERE t.call_id = calls.id
                 AND t.action IN ('faq', 'answer')
                 AND trim(t.answer) != '') > 0 THEN 'bot'
        ELSE 'dropped' END;
    CREATE INDEX turns_faq ON turns(faq_id);
    """,
]

_local = threading.local()


def now() -> str:
    """Timestamps are stored as ISO strings in local time: they are read by people."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Opens the database, applying the settings that make concurrent use safe."""
    target = Path(path or DEFAULT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def migrate(connection: sqlite3.Connection) -> int:
    """Brings the database up to date. Returns the schema version it ended on.

    One step, one transaction, version stamp included. That takes care: the obvious
    `executescript` inside a transaction block does not give it, because `executescript`
    commits whatever is open before it runs a thing. A step that failed halfway therefore
    used to leave its first half committed and the version unchanged — so every later
    start replayed it, hit "table already exists", and the database could never be opened
    again.

    The version is read inside the same `BEGIN IMMEDIATE` that applies the step, so the
    bridge and the CRM starting together cannot both decide to apply the same one.
    """
    while True:
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version >= len(MIGRATIONS):
                connection.execute("COMMIT")
                return version
            for statement in _statements(MIGRATIONS[version]):
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version={version + 1}")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise


def _statements(script: str) -> list[str]:
    """Splits a migration into statements, keeping `CREATE TRIGGER … BEGIN … END;` whole.

    `executescript` would do the splitting for us, but it commits first — see `migrate`.
    `sqlite3.complete_statement` is the same test SQLite's own shell uses to decide
    whether it has a whole statement yet, so triggers survive it.
    """
    statements: list[str] = []
    current = ""
    for line in script.splitlines(keepends=True):
        current += line
        if current.strip() and sqlite3.complete_statement(current):
            statements.append(current)
            current = ""
    if current.strip():
        statements.append(current)
    return statements


def open_database(path: Path | str | None = None) -> sqlite3.Connection:
    connection = connect(path)
    migrate(connection)
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All or nothing. Nested use is allowed: only the outermost block commits."""
    if getattr(_local, "depth", 0):
        _local.depth += 1
        try:
            yield connection
        finally:
            _local.depth -= 1
        return
    connection.execute("BEGIN IMMEDIATE")
    _local.depth = 1
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        _local.depth = 0
