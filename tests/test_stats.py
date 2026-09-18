"""The numbers a supervisor reads, checked against numbers counted by hand.

Every figure here was wrong at some point in a way that flattered the project: abandoned
calls counted as successes, a backlog that made the busiest operator look idle, one
caller's fifty questions presented as what the country asks about. A wrong number on
this screen is worse than a missing one, because nobody doubts it.
"""

import pytest

from crm import repo, stats
from crm.security import hash_password
from store.db import open_database
from store.write import save_call


def call(call_id: str, *, started: str, ended: str, next_action: str = "operator", turns=()):
    return {
        "call_id": call_id,
        "caller": f"99890{call_id}",
        "started_at": started,
        "duration_s": 30,
        "next_action": next_action,
        "ended_reason": ended,
        "turns": list(turns),
    }


def answered(faq_id="stipend", ms=500.0, intent="faq_keyword", question=None):
    return {
        "question": question or f"{faq_id} haqida savol",
        "answer": "Javob matni.",
        "action": "faq",
        "intent": intent,
        "faq_id": faq_id,
        "latency_ms": {"total_ms": ms},
    }


def not_understood(ms=520.0):
    return {
        "question": "shovqin",
        "answer": "Tushunmadim.",
        "action": "clarify",
        "intent": "unclear",
        "faq_id": "",
        "latency_ms": {"total_ms": ms},
    }


@pytest.fixture
def db(tmp_path):
    return open_database(tmp_path / "stats.db")


def test_the_two_screens_never_disagree_about_calls_handled_without_a_person(db):
    """The dashboard and the analytics tile carry the same label and must carry the same
    number. One of them used to count abandoned calls as successes, so the worse the day
    went, the better it read — and that was the screen meant to be the serious one."""
    today = stats.today()
    save_call(
        db,
        call(
            "1",
            started=f"{today} 09:00:00",
            ended="goodbye",
            next_action="hangup",
            turns=[answered()],
        ),
    )
    for n in range(5):  # gave up during the greeting: nothing was ever said to them
        save_call(db, call(f"g{n}", started=f"{today} 09:1{n}:00", ended="caller_hangup"))
    for n in range(2):
        save_call(db, call(f"t{n}", started=f"{today} 09:2{n}:00", ended="transfer"))

    board = stats.dashboard(db, user_id=0)
    screen = stats.analytics(db, days=7)
    totals = screen["totals"]

    assert board["calls_today"] == 8
    assert board["without_operator_percent"] == 12, "1 of 8 was actually handled"
    assert round(100 * totals["bot"] / totals["calls"]) == board["without_operator_percent"]
    assert (totals["bot"], totals["operator"], totals["dropped"]) == (1, 2, 5)


def test_a_call_where_nothing_was_understood_is_not_counted_as_handled(db):
    """It produces a turn, with a latency, and the caller got nothing. Counting it left
    them out of the follow-up list as well."""
    today = stats.today()
    save_call(
        db, call("x", started=f"{today} 10:00:00", ended="caller_hangup", turns=[not_understood()])
    )

    assert stats.dashboard(db, user_id=0)["without_operator_percent"] == 0
    assert stats.analytics(db, days=7)["totals"]["dropped"] == 1


def test_one_persistent_caller_does_not_become_the_nation_s_top_question(db):
    """Grouping by turn alone, fifty calls from one number decided what the ministry
    believed citizens were asking about."""
    today = stats.today()
    for n in range(20):
        entry = call(
            f"same{n}",
            started=f"{today} 11:00:00",
            ended="goodbye",
            next_action="hangup",
            turns=[answered()],
        )
        entry["caller"] = "998900000001"  # the same person, over and over
        save_call(db, entry)
    save_call(
        db,
        call(
            "other",
            started=f"{today} 11:30:00",
            ended="goodbye",
            next_action="hangup",
            turns=[answered(faq_id="academic_leave")],
        ),
    )

    questions = stats.analytics(db, days=7)["questions"]

    top = questions[0]
    assert top["count"] == 20 and top["people"] == 1, "the screen must show it was one caller"


def test_how_a_call_ended_is_not_a_topic(db):
    """ "Wanted an operator" and "could not understand" are outcomes, not subjects; in the
    same chart a supervisor cannot tell which answers are missing."""
    today = stats.today()
    save_call(
        db,
        call(
            "a",
            started=f"{today} 12:00:00",
            ended="goodbye",
            next_action="hangup",
            turns=[answered()],
        ),
    )
    save_call(
        db, call("b", started=f"{today} 12:05:00", ended="caller_hangup", turns=[not_understood()])
    )

    topics = [row["topic"] for row in stats.analytics(db, days=7)["topics"]]

    assert topics == ["stipend"], f"outcomes leaked into the topics chart: {topics}"


def test_the_typical_answer_is_not_destroyed_by_one_slow_turn(db):
    """Four answers under a second and one that crawled used to read as twelve seconds."""
    today = stats.today()
    for n, ms in enumerate((300.0, 400.0, 500.0, 800.0, 62000.0)):
        save_call(
            db,
            call(
                f"m{n}",
                started=f"{today} 13:0{n}:00",
                ended="goodbye",
                next_action="hangup",
                turns=[answered(ms=ms)],
            ),
        )

    board = stats.dashboard(db, user_id=0)

    assert board["avg_answer_ms"] == 500, "the tile should show the typical answer"
    assert board["slowest_answer_ms"] == 62000, "and say plainly that one took a minute"


def test_an_operator_working_through_an_old_backlog_does_not_look_idle(db):
    """The table counted tickets *created* in the period, so the oldest and heaviest load
    — exactly what it exists to show — was invisible, and contradicted the tile above."""
    boss = repo.create_user(db, "boss", "Boss", "supervisor", hash_password("x" * 10), "")
    old = repo.create_ticket(db, subject="Eski", body="", author_id=boss, assignee_id=boss)
    db.execute("UPDATE tickets SET created_at = '2026-07-01 09:00:00' WHERE id = ?", (old,))

    screen = stats.analytics(db, days=7)

    assert screen["tickets"]["open"] == 1
    assert [row["open"] for row in screen["operators"]] == [1], "the backlog vanished"


@pytest.mark.parametrize("count", [1, 3, 10, 100])
def test_p90_is_a_percentile_and_not_simply_the_worst_answer(db, count):
    today = stats.today()
    for n in range(count):
        save_call(
            db,
            call(
                f"p{n}",
                started=f"{today} 14:00:00",
                ended="goodbye",
                next_action="hangup",
                turns=[answered(ms=float(n + 1))],
            ),
        )

    p90 = stats.analytics(db, days=7)["latency"]["p90"]

    expected = sorted(float(n + 1) for n in range(count))[max(0, -(-count * 9 // 10) - 1)]
    assert p90 == round(expected)


def test_nothing_divides_by_zero_on_a_day_with_no_calls(db):
    board = stats.dashboard(db, user_id=0)
    screen = stats.analytics(db, days=30)

    assert board["without_operator_percent"] == 0 and board["avg_answer_ms"] == 0
    assert screen["totals"]["calls"] == 0 and screen["latency"]["p90"] == 0
    assert len(screen["by_day"]) == 30 and len(screen["by_hour"]) == 24
