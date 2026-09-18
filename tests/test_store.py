"""The shared database: schema, writing calls, importing the old journals."""

import json
import sqlite3

import pytest

from store.db import migrate, open_database
from store.knowledge import PublishedKnowledge
from store.write import CallStore, import_jsonl, outcome_of, save_call


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path / "test.db")
    yield connection
    connection.close()


def entry(call_id="a", **overrides):
    base = {
        "call_id": call_id,
        "caller": "998901234567",
        "started_at": "2026-09-18 10:00:00",
        "duration_s": 42.0,
        "next_action": "hangup",
        "ended_reason": "goodbye",
        "recording": f"eduvoice/20260918/{call_id}.wav",
        "turns": [
            {
                "question": "stipendiya qanday olinadi",
                "answer": "Stipendiya javobi",
                "intent": "faq_keyword",
                "action": "faq",
                "faq_id": "stipend",
                "source": "lex.uz",
                "at_ms": 4300.0,
                "latency_ms": {"stt_ms": 300.0, "total_ms": 320.0},
            }
        ],
    }
    return {**base, **overrides}


def test_migrations_create_the_schema_and_are_repeatable(tmp_path):
    connection = open_database(tmp_path / "x.db")

    first = connection.execute("PRAGMA user_version").fetchone()[0]
    second = migrate(connection)  # running again must change nothing

    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert first == second
    assert {"users", "contacts", "calls", "turns", "tickets", "knowledge", "audit"} <= tables


def test_a_call_is_stored_with_its_turns_and_its_citizen(db):
    save_call(db, entry())

    call = db.execute("SELECT * FROM calls").fetchone()
    turn = db.execute("SELECT * FROM turns").fetchone()
    contact = db.execute("SELECT * FROM contacts").fetchone()

    assert call["outcome"] == "bot"
    assert call["questions"] == 1
    assert call["avg_answer_ms"] == 320.0
    assert turn["faq_id"] == "stipend" and turn["at_ms"] == 4300.0
    assert contact["phone"] == "998901234567"
    assert call["contact_id"] == contact["id"]


def test_the_same_citizen_gets_one_card_for_many_calls(db):
    save_call(db, entry("a"))
    save_call(db, entry("b"))

    assert db.execute("SELECT count(*) FROM contacts").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM calls").fetchone()[0] == 2


def test_saving_a_call_twice_does_not_duplicate_it(db):
    save_call(db, entry())
    save_call(db, entry())

    assert db.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1


def test_outcomes_tell_the_three_cases_apart():
    assert outcome_of("hangup", "goodbye", 1) == "bot"
    assert outcome_of("operator", "transfer", 0) == "operator"
    assert outcome_of("operator", "caller_hangup", 0) == "dropped"


def test_a_turn_that_delivered_nothing_is_not_an_answer():
    """Every service phrase also makes a turn, and each one carries a measured latency.

    Counting turns, or turns with a latency, scored "I did not understand you" and "I am
    transferring you" as questions answered — so a caller who got nothing useful and hung
    up in frustration was filed as handled, and vanished from the follow-up worklist.
    """
    from store.write import delivered_answers

    latency = {"total_ms": 520.0}
    assert (
        delivered_answers([{"action": "clarify", "answer": "Tushunmadim.", "latency_ms": latency}])
        == 0
    )
    assert (
        delivered_answers([{"action": "transfer", "answer": "Ulayman.", "latency_ms": latency}])
        == 0
    )
    assert (
        delivered_answers([{"action": "goodbye", "answer": "Sogʻ boʻling.", "latency_ms": latency}])
        == 0
    )
    assert (
        delivered_answers([{"action": "faq", "answer": "Stipendiya…", "latency_ms": latency}]) == 1
    )
    assert delivered_answers([{"action": "answer", "answer": "", "latency_ms": latency}]) == 0

    # The whole truth table the bridge can produce.
    assert outcome_of("operator", "transfer", 0) == "operator"
    assert outcome_of("operator", "tech_problem", 1) == "operator"
    assert outcome_of("hangup", "goodbye", 0) == "dropped", "nothing was said to the caller"
    assert outcome_of("hangup", "goodbye", 1) == "bot"
    assert outcome_of("operator", "caller_hangup", 0) == "dropped"
    assert outcome_of("operator", "caller_hangup", 2) == "bot"


def test_hanging_up_after_the_answer_counts_as_served():
    """The commonest happy ending on a real line: the answer is heard, the call ends.

    `next_action` is still "operator" at that moment — that is the safety default, not a
    transfer — so only the delivered answer distinguishes this from someone who gave up
    during the greeting.
    """
    assert outcome_of("operator", "caller_hangup", 1) == "bot"
    assert outcome_of("operator", "caller_hangup", 0) == "dropped"


def test_transcripts_are_searchable(db):
    save_call(db, entry("a"))
    save_call(db, entry("b", turns=[{"question": "akademik taʼtil", "answer": "javob"}]))

    found = db.execute(
        "SELECT question FROM turns_fts WHERE turns_fts MATCH ?", ("stipendiya",)
    ).fetchall()

    assert len(found) == 1


def test_the_bridge_never_fails_a_call_because_of_the_database(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a database")

    CallStore(blocked).save(entry())  # must not raise


def test_history_from_the_json_journals_is_imported(tmp_path, db):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "20260918.jsonl").write_text(
        "\n".join(json.dumps(entry(name)) for name in ("a", "b")) + "\n{ broken\n",
        encoding="utf-8",
    )

    imported = import_jsonl(logs, db)

    assert imported == 2
    assert db.execute("SELECT count(*) FROM calls").fetchone()[0] == 2


def test_only_published_answers_reach_the_assistant(tmp_path, db):
    db.execute(
        "INSERT INTO knowledge (faq_id, question, answer, keywords, status, created_at, updated_at)"
        " VALUES ('stipend', 'q', 'Published answer', 'stipendiya', 'published', 'now', 'now')"
    )
    db.execute(
        "INSERT INTO knowledge (faq_id, question, answer, status, created_at, updated_at)"
        " VALUES ('draft_one', 'q', 'Draft answer', 'draft', 'now', 'now')"
    )
    db.commit()

    entries = PublishedKnowledge(tmp_path / "test.db").entries()

    assert set(entries) == {"stipend"}
    assert entries["stipend"]["keywords"] == ["stipendiya"]


def test_reimporting_a_call_keeps_the_ticket_attached_to_it(tmp_path):
    """`import-history` is documented as safe to run twice, and an operator may well do it.

    Re-saving a call deletes and reinserts the row, and tickets reference it with
    ON DELETE SET NULL — so the second run used to cut every ticket loose from its
    conversation. The recording and the transcript disappear from the ticket, and the
    call reappears on the "nobody has handled this" list as if it were new.
    """
    from crm import repo
    from crm.security import hash_password
    from store.db import open_database
    from store.write import save_call

    db = open_database(tmp_path / "again.db")
    author = repo.create_user(db, "boss", "Boss", "supervisor", hash_password("x" * 10), "")
    entry = {
        "call_id": "call-xyz",
        "caller": "998901112233",
        "started_at": "2026-09-18 10:00:00",
        "duration_s": 42,
        "next_action": "hangup",
        "ended_reason": "goodbye",
        "turns": [{"question": "Stipendiya?", "answer": "Javob.", "action": "faq"}],
    }
    call_id = save_call(db, entry)
    ticket_id = repo.create_ticket(db, subject="S", body="", author_id=author, call_pk=call_id)

    save_call(db, entry)  # the very same journal line, imported a second time

    attached = db.execute("SELECT call_id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    assert attached["call_id"] is not None, "the ticket lost the call it came from"
    assert (
        attached["call_id"]
        == db.execute("SELECT id FROM calls WHERE call_id = 'call-xyz'").fetchone()["id"]
    )


def test_a_migration_that_fails_halfway_leaves_nothing_behind(tmp_path, monkeypatch):
    """Otherwise the database can never be opened again — the worst outcome there is.

    `executescript` commits whatever is open before it runs, so a step that failed in the
    middle used to leave its first half committed with the version unchanged. Every later
    start replayed the same step, hit "table already exists", and gave up.
    """
    import store.db as db_module
    from store.db import connect, migrate

    monkeypatch.setattr(
        db_module,
        "MIGRATIONS",
        ["CREATE TABLE good (x TEXT);", "CREATE TABLE half (x TEXT);\nCREATE TABLE half (x TEXT);"],
    )
    db = connect(tmp_path / "broken.db")

    with pytest.raises(sqlite3.OperationalError):
        migrate(db)

    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert "half" not in tables, "half of a failed migration stayed behind"
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1, "the first step should hold"
    assert not db.in_transaction, "the connection was left inside a transaction"
