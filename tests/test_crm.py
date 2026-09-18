"""The CRM over HTTP: who is let in, what they may do, and the paths that matter.

These tests drive the real application through a client, so a broken template or a wrong
redirect fails here and not in front of a supervisor.
"""

import struct
import wave

import pytest
from fastapi.testclient import TestClient

from crm import repo
from crm.app import create_app
from crm.config import settings
from crm.security import hash_password
from store.db import open_database
from store.knowledge import PublishedKnowledge
from store.write import save_call

PASSWORD = "secret-123"


def make_wav(path, seconds: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(struct.pack("<h", 900) * int(8000 * seconds))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A database with three people, one call and its recording."""
    monkeypatch.setattr(settings, "recordings_dir", tmp_path / "rec")
    db = open_database(tmp_path / "crm.db")
    for login, role in (("operator", "operator"), ("boss", "supervisor"), ("root", "admin")):
        repo.create_user(db, login, login.title(), role, hash_password(PASSWORD), extension="101")
    save_call(
        db,
        {
            "call_id": "call-one",
            "caller": "998901234567",
            "started_at": "2026-09-18 10:00:00",
            "duration_s": 30.0,
            "next_action": "operator",
            "ended_reason": "transfer",
            "recording": "eduvoice/20260918/call-one.wav",
            "turns": [
                {
                    "question": "stipendiya qanday olinadi",
                    "answer": "Stipendiya javobi",
                    "intent": "faq_keyword",
                    "action": "faq",
                    "faq_id": "stipend",
                    "at_ms": 4000.0,
                    "latency_ms": {"total_ms": 300.0},
                }
            ],
        },
    )
    make_wav(tmp_path / "rec" / "eduvoice" / "20260918" / "call-one.wav")
    db.close()
    yield tmp_path / "crm.db"


@pytest.fixture
def client(world):
    return TestClient(create_app(world), follow_redirects=False)


def sign_in(client: TestClient, login: str = "operator") -> None:
    """Signs in the way a browser does: open the form, then post it with its token."""
    form = client.get("/login")
    marker = 'name="csrf" value="'
    start = form.text.index(marker) + len(marker)
    csrf = form.text[start : form.text.index('"', start)]
    response = client.post(
        "/login", data={"login": login, "password": PASSWORD, "next": "/", "csrf": csrf}
    )
    assert response.status_code == 303, "login should redirect on success"


def token(client: TestClient) -> str:
    """Reads the form token out of a page, the way a browser would."""
    page = client.get("/tickets/new").text
    marker = 'name="csrf" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


# ------------------------------------------------------------------- access


def test_a_stranger_sees_nothing(client):
    for path in ("/", "/calls", "/tickets", "/contacts", "/knowledge", "/analytics", "/admin"):
        response = client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


def test_a_login_form_from_another_site_is_refused(client):
    """Login CSRF: otherwise a victim can be signed into an attacker's account."""
    response = client.post(
        "/login", data={"login": "operator", "password": PASSWORD, "csrf": "forged"}
    )

    assert response.status_code == 200
    assert "eduvoice_session" not in response.cookies


@pytest.mark.parametrize("target", ["//evil.example/", "/\\evil.example", "https://evil.example"])
def test_login_never_redirects_to_another_site(client, target):
    form = client.get("/login", params={"next": target})
    marker = 'name="csrf" value="'
    start = form.text.index(marker) + len(marker)
    csrf = form.text[start : form.text.index('"', start)]

    response = client.post(
        "/login", data={"login": "operator", "password": PASSWORD, "next": target, "csrf": csrf}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_a_wrong_password_does_not_sign_anybody_in(client):
    form = client.get("/login")
    marker = 'name="csrf" value="'
    start = form.text.index(marker) + len(marker)
    csrf = form.text[start : form.text.index('"', start)]
    response = client.post("/login", data={"login": "operator", "password": "nope", "csrf": csrf})

    assert response.status_code == 200
    assert "eduvoice_session" not in response.cookies
    assert client.get("/").status_code == 303


def test_signing_in_opens_the_dashboard(client):
    sign_in(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "EduVoice" in response.text


def test_an_operator_may_not_open_administration(client):
    sign_in(client, "operator")

    assert client.get("/admin").status_code == 403
    assert client.get("/analytics").status_code == 403


def test_a_supervisor_sees_analytics_but_not_administration(client):
    sign_in(client, "boss")

    assert client.get("/analytics").status_code == 200
    assert client.get("/admin").status_code == 403


def test_an_administrator_sees_everything(client):
    sign_in(client, "root")

    assert client.get("/admin").status_code == 200
    assert client.get("/analytics").status_code == 200


def test_a_form_without_its_token_is_refused(client):
    """Without this check another site could create requests in a supervisor's name."""
    sign_in(client)

    response = client.post("/tickets/new", data={"subject": "x", "csrf": "forged"})

    assert response.status_code == 403


# -------------------------------------------------------------------- work


def test_a_call_is_listed_searched_and_opened(client, world):
    sign_in(client)

    listing = client.get("/calls")
    found = client.get("/calls", params={"q": "stipendiya"})
    nothing = client.get("/calls", params={"q": "hech-narsa-yoq"})
    detail = client.get("/calls/1")

    assert "998901234567" in listing.text
    assert "stipendiya qanday olinadi" in found.text
    assert "stipendiya qanday olinadi" not in nothing.text
    assert detail.status_code == 200
    assert "Stipendiya javobi" in detail.text


def test_the_recording_plays_and_can_be_seeked(client):
    sign_in(client)

    whole = client.get("/media/recordings/call-one")
    part = client.get("/media/recordings/call-one", headers={"Range": "bytes=100-199"})

    assert whole.status_code == 200
    assert whole.headers["content-type"] == "audio/wav"
    assert whole.content[:4] == b"RIFF"
    assert part.status_code == 206
    assert len(part.content) == 100


@pytest.mark.parametrize(
    "path",
    ["/media/recordings/../../../etc/passwd", "/media/recordings/%2e%2e%2fpasswd"],
)
def test_no_other_file_can_be_served(client, path):
    sign_in(client)

    response = client.get(path)

    assert response.status_code == 404
    assert b"root:" not in response.content


def test_a_call_becomes_a_request_with_one_click(client, world):
    sign_in(client)
    csrf = token(client)

    created = client.post(
        "/calls/1/ticket",
        data={"subject": "Stipendiya haqida", "body": "", "priority": "high", "csrf": csrf},
    )

    assert created.status_code == 303
    db = open_database(world)
    ticket = repo.tickets(db)[0]
    assert ticket["subject"] == "Stipendiya haqida"
    assert ticket["priority"] == "high"
    assert ticket["call_id"] == 1
    assert ticket["contact_phone"] == "998901234567", "the citizen is linked automatically"
    assert [event["kind"] for event in repo.ticket_events(db, ticket["id"])] == ["created"]
    db.close()


def test_the_whole_life_of_a_request_is_recorded(client, world):
    sign_in(client, "boss")
    csrf = token(client)
    client.post(
        "/tickets/new", data={"subject": "Akademik taʼtil", "phone": "998911112233", "csrf": csrf}
    )
    db = open_database(world)
    ticket_id = repo.tickets(db)[0]["id"]

    client.post(
        f"/tickets/{ticket_id}", data={"status": "in_progress", "assignee_id": "2", "csrf": csrf}
    )
    client.post(
        f"/tickets/{ticket_id}/comment", data={"text": "Fuqaroga qoʻngʻiroq qilindi", "csrf": csrf}
    )
    client.post(
        f"/tickets/{ticket_id}",
        data={"status": "resolved", "resolution": "Tushuntirildi", "csrf": csrf},
    )

    ticket = repo.ticket(db, ticket_id)
    kinds = [event["kind"] for event in repo.ticket_events(db, ticket_id)]
    assert ticket["status"] == "resolved"
    assert ticket["resolution"] == "Tushuntirildi"
    assert ticket["resolved_at"]
    assert kinds == ["created", "status", "assign", "comment", "status"]
    db.close()


def test_a_citizen_card_collects_calls_and_requests(client, world):
    sign_in(client)
    csrf = token(client)
    client.post("/calls/1/ticket", data={"subject": "Savol", "csrf": csrf})

    page = client.get("/contacts/1")

    assert page.status_code == 200
    assert "998901234567" in page.text
    assert "Savol" in page.text


# --------------------------------------------------------------- knowledge


def test_publishing_an_answer_changes_what_the_assistant_says(client, world):
    """The point of putting the knowledge base in the CRM: no deploy, no restart."""
    sign_in(client, "boss")
    csrf = token(client)
    client.post(
        "/knowledge/new",
        data={
            "faq_id": "stipend",
            "question": "Stipendiya",
            "answer": "Yangi javob",
            "keywords": "stipendiya",
            "source": "lex.uz 12-band",
            "csrf": csrf,
        },
    )
    knowledge = PublishedKnowledge(world)

    assert knowledge.entries() == {}, "a draft must never reach a caller"

    client.post("/knowledge/1/status", data={"status": "published", "csrf": csrf})
    knowledge._checked_at = 0.0  # the bridge would reach this point a few seconds later

    entries = knowledge.entries()
    assert entries["stipend"]["answer"] == "Yangi javob"
    assert entries["stipend"]["keywords"] == ["stipendiya"]


def test_an_operator_may_read_the_knowledge_base_but_not_change_it(client):
    sign_in(client, "operator")
    csrf = token(client)

    assert client.get("/knowledge").status_code == 200
    assert (
        client.post("/knowledge/new", data={"faq_id": "x", "answer": "y", "csrf": csrf}).status_code
        == 403
    )


def test_editing_an_answer_keeps_the_previous_version(client, world):
    sign_in(client, "boss")
    csrf = token(client)
    client.post("/knowledge/new", data={"faq_id": "stipend", "answer": "Birinchi", "csrf": csrf})

    client.post("/knowledge/1", data={"answer": "Ikkinchi", "csrf": csrf})

    db = open_database(world)
    entry = repo.knowledge_entry(db, 1)
    versions = repo.knowledge_versions(db, 1)
    assert entry["answer"] == "Ikkinchi" and entry["version"] == 2
    assert versions[0]["answer"] == "Birinchi", "the old wording must stay on record"
    db.close()


# ------------------------------------------------------------------- admin


def test_administration_creates_people_and_writes_down_who_did_it(client, world):
    sign_in(client, "root")
    csrf = token(client)

    client.post(
        "/admin/users",
        data={
            "login": "yangi",
            "name": "Yangi operator",
            "role": "operator",
            "password": "parol-123",
            "extension": "102",
            "csrf": csrf,
        },
    )

    db = open_database(world)
    created = repo.user_by_login(db, "yangi")
    actions = [row["action"] for row in repo.audit_log(db)]
    assert created is not None and created["role"] == "operator"
    assert "user_create" in actions
    assert "login" in actions, "every sign-in is recorded"
    db.close()


def test_health_needs_no_password(client):
    assert client.get("/health").json()["status"] == "ok"


def test_the_call_list_is_paged(client, world):
    """A hundred thousand calls must never be rendered into one page."""
    db = open_database(world)
    for number in range(120):
        save_call(
            db,
            {
                "call_id": f"bulk-{number}",
                "caller": "998900000000",
                "started_at": f"2026-09-18 {number % 24:02d}:00:00",
                "duration_s": 10.0,
                "next_action": "hangup",
                "ended_reason": "goodbye",
                "turns": [],
            },
        )
    db.close()
    sign_in(client)

    first = client.get("/calls")
    second = client.get("/calls", params={"page_no": 2})

    assert first.text.count('href="/calls/') == 100, "a page shows exactly one hundred calls"
    assert "page_no=2" in first.text, "there must be a way to the next page"
    assert second.status_code == 200
    assert second.text.count('href="/calls/') > 0


def test_an_empty_recording_is_not_served_as_a_broken_stream(client, world, tmp_path):
    """A zero-byte file used to be answered with one byte of nothing: the player hung."""
    empty = tmp_path / "rec" / "eduvoice" / "20260918" / "call-one.wav"
    empty.write_bytes(b"")
    sign_in(client)

    assert client.get("/media/recordings/call-one").status_code == 404


def test_a_background_refresh_after_the_session_ended_says_so(client):
    """The dashboard must send the operator to the login, not freeze on old numbers."""
    response = client.get("/api/dashboard")

    assert response.status_code == 401
    assert response.json()["error"] == "login_required"


def test_a_duplicate_login_is_refused_without_crashing(client, world):
    sign_in(client, "root")
    csrf = token(client)

    response = client.post(
        "/admin/users",
        data={
            "login": "operator",
            "name": "Another",
            "role": "operator",
            "password": "x-123456",
            "csrf": csrf,
        },
    )

    assert response.status_code == 303
    db = open_database(world)
    assert db.execute("SELECT count(*) FROM users WHERE login = 'operator'").fetchone()[0] == 1
    db.close()


def test_two_answers_published_in_the_same_second_are_both_noticed(client, world):
    """The bridge polls a fingerprint: it must change on every publish, not every second."""
    from store.knowledge import PublishedKnowledge

    sign_in(client, "boss")
    csrf = token(client)
    client.post("/knowledge/new", data={"faq_id": "one", "answer": "A", "csrf": csrf})
    client.post("/knowledge/new", data={"faq_id": "two", "answer": "B", "csrf": csrf})
    client.post("/knowledge/1/status", data={"status": "published", "csrf": csrf})
    knowledge = PublishedKnowledge(world)
    first = dict(knowledge.entries())

    client.post("/knowledge/2/status", data={"status": "published", "csrf": csrf})
    knowledge._checked_at = 0.0

    assert set(first) == {"one"}
    assert set(knowledge.entries()) == {"one", "two"}, "the second publish was missed"


def test_an_overdue_request_is_marked_as_such(client, world):
    db = open_database(world)
    db.execute(
        "INSERT INTO tickets (number, subject, status, priority, due_at, created_at, updated_at)"
        " VALUES ('2026-9999', 'Kechikkan', 'new', 'normal', '2020-01-01 10:00:00', ?, ?)",
        ("2026-09-18 10:00:00", "2026-09-18 10:00:00"),
    )
    db.commit()
    tickets = repo.tickets(db)
    db.close()

    assert tickets[0]["overdue"] == 1, "the deadline passed years ago"


# ---------------------------------------------------- looking inside the database


def test_the_sql_helper_reads_but_refuses_to_write(tmp_path, monkeypatch, capsys):
    """The server's own sqlite3 is from 2013 and calls this database malformed.

    So the project carries its own way in. It is meant for looking, not for changing, and
    the guard is the connection rather than the wording of the query: SQLite runs
    `WITH … DELETE` quite happily, and one `PRAGMA user_version = 0` leaves a file that
    nothing can open again — including the bridge, which would then stop recording calls
    without saying so.
    """
    from crm import cli
    from crm.config import settings as crm_settings

    database = tmp_path / "look.db"
    repo.create_user(
        open_database(database), "boss", "Boss", "supervisor", hash_password(PASSWORD), ""
    )
    monkeypatch.setattr(crm_settings, "db_path", database)

    assert cli.main(["sql", "SELECT login FROM users"]) == 0
    assert "boss" in capsys.readouterr().out

    for attack in (
        "DELETE FROM users",
        "WITH x AS (SELECT 1) DELETE FROM users",
        "WITH x AS (SELECT 1) UPDATE knowledge SET answer = 'soxta', status = 'published'",
        "PRAGMA user_version = 0",
    ):
        assert cli.main(["sql", attack]) == 1, f"{attack!r} was not refused"

    survivor = open_database(database)
    assert survivor.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    assert survivor.execute("PRAGMA user_version").fetchone()[0] > 0, "the schema was wiped"


# ------------------------------------------------- input nobody types on purpose


def test_odd_input_gives_a_page_rather_than_a_traceback(client):
    """Values a browser can produce but a form never offers: a stray character in a
    select box, a page number pasted from nowhere. Each one used to reach `int()` bare
    and come back as an unhandled 500 with a traceback on screen."""
    sign_in(client, "boss")

    assert client.get("/calls?page_no=99999999999999999999").status_code == 200
    assert client.get("/calls?page_no=-4").status_code == 200

    csrf = token(client)
    answer = client.post(
        "/tickets/1", data={"csrf": csrf, "assignee_id": "x", "status": "in_progress"}
    )
    assert answer.status_code in (303, 404), f"unexpected {answer.status_code}"


def test_a_search_with_a_hash_keeps_its_filters_on_the_next_page(client):
    """Everything after `#` is a fragment the browser never sends.

    Unencoded, the pager link dropped the day filter and silently returned page one.
    """
    sign_in(client, "boss")

    page = client.get("/calls?q=stipendiya%23&day=2026-09-18&page_no=2").text

    assert "q=stipendiya%23" in page, "the search term was not encoded into the pager link"
    assert "#" not in page[page.index("pager") : page.index("pager") + 400]


def test_logging_out_needs_a_form_and_a_token(client):
    """`<img src="/logout">` on any page would otherwise sign the operator out."""
    sign_in(client)

    assert client.get("/logout").status_code == 405, "logout must not answer a GET"
    assert client.post("/logout", data={"csrf": "wrong"}).headers["location"] == "/"
    assert client.get("/calls").status_code == 200, "the session should have survived"

    assert client.post("/logout", data={"csrf": token(client)}).headers["location"] == "/login"
    assert client.get("/calls").status_code == 303, "the session should be gone"


def test_a_planned_callback_shows_on_the_dashboard(client, world):
    """A promise to ring a citizen back was recorded, counted, and then shown nowhere.

    It only appeared inside one contact's card, so an operator who made the promise had
    no list of them anywhere and could simply forget.
    """
    from crm import repo

    db = open_database(world)
    operator = repo.user_by_login(db, "operator")
    contact = repo.contacts(db, limit=1)[0]
    repo.create_callback(
        db,
        contact_id=int(contact["id"]),
        due_at="2026-09-19 09:00",
        assignee_id=int(operator["id"]),
    )
    db.close()

    sign_in(client, "operator")
    page = client.get("/").text

    assert "19.09 09:00" in page, "the planned callback is not on the dashboard"


# ------------------------------------------------------------- administration


def test_the_last_administrator_cannot_sign_away_their_own_rights(client, world):
    """There is no way back from it: the rights to restore an administrator are the ones
    just given up. On a ministry server that means editing the database by hand."""
    from crm import repo

    sign_in(client, "root")
    db = open_database(world)
    admin_id = int(repo.user_by_login(db, "root")["id"])
    db.close()
    csrf = token(client)

    client.post(
        f"/admin/users/{admin_id}",
        data={"csrf": csrf, "name": "Root", "role": "operator", "active": "1"},
    )
    client.post(f"/admin/users/{admin_id}", data={"csrf": csrf, "name": "Root", "role": "admin"})

    db = open_database(world)
    still = repo.user_by_login(db, "root")
    db.close()
    assert still["role"] == "admin", "the last administrator demoted themselves"
    assert still["active"] == 1, "the last administrator switched their own account off"
    assert client.get("/admin").status_code == 200, "administration is now unreachable"


def test_a_new_account_needs_a_password_worth_having(client, world):
    """The form accepted a single character, and that account can open every call record
    of every citizen who has ever rung the ministry."""
    from crm import repo

    sign_in(client, "root")

    answer = client.post(
        "/admin/users",
        data={
            "csrf": token(client),
            "login": "qisqa",
            "name": "Q",
            "role": "operator",
            "password": "1",
        },
    )

    assert "weak_password" in answer.headers.get("location", "")
    db = open_database(world)
    assert repo.user_by_login(db, "qisqa") is None, "a one-character password was accepted"
    db.close()


def test_handing_a_ticket_to_an_account_that_no_longer_exists_says_so(client):
    """A page left open while an account was removed, or a replayed form. Straight
    through, it hit the foreign key and came back as "the database is unavailable"."""
    sign_in(client, "boss")

    answer = client.post(
        "/tickets/new",
        data={"csrf": token(client), "subject": "S", "body": "B", "assignee_id": "99999"},
    )

    assert answer.status_code == 303, f"unexpected {answer.status_code}"
    ticket = client.get(answer.headers["location"])
    assert ticket.status_code == 200


def test_publishing_an_answer_does_not_invent_a_new_version_of_its_text(client, world):
    """The history exists to answer one question: how did the wording change.

    Publishing and unpublishing used to file entries whose text was identical to the last,
    so six clicks on the button buried the one edit that mattered. Who published it, and
    when, is in the audit log, where it belongs.
    """
    from crm import repo

    sign_in(client, "boss")
    db = open_database(world)
    entry = repo.create_knowledge(
        db,
        faq_id="sinov",
        question="Savol",
        answer="Birinchi javob.",
        keywords="sinov",
        source="59-son",
        author_id=1,
    )
    db.close()
    csrf = token(client)

    for status in ("published", "draft", "published"):
        client.post(f"/knowledge/{entry}/status", data={"csrf": csrf, "status": status})
    client.post(
        f"/knowledge/{entry}",
        data={
            "csrf": csrf,
            "question": "Savol",
            "answer": "Ikkinchi javob.",
            "keywords": "sinov",
            "source": "59-son",
        },
    )

    db = open_database(world)
    versions = repo.knowledge_versions(db, entry)
    live = repo.knowledge_entry(db, entry)
    db.close()
    assert len(versions) == 1, f"the history has {len(versions)} entries for one rewording"
    assert versions[0]["answer"] == "Birinchi javob."
    assert live["answer"] == "Ikkinchi javob." and live["status"] == "published"


def test_a_promised_callback_can_be_marked_as_kept(client, world):
    """It could be promised but never closed: the row stayed "planned" for ever, on the
    operator's dashboard and in the citizen's card, with no way to say it was done."""
    from crm import repo

    db = open_database(world)
    contact = repo.contacts(db, limit=1)[0]
    operator = repo.user_by_login(db, "operator")
    callback = repo.create_callback(
        db,
        contact_id=int(contact["id"]),
        due_at="2026-09-20 09:00",
        assignee_id=int(operator["id"]),
    )
    db.close()
    sign_in(client, "operator")
    assert "20.09 09:00" in client.get("/").text, "the promise should be on the dashboard"

    answer = client.post(
        f"/contacts/{contact['id']}/callback/{callback}",
        data={"csrf": token(client), "result": "Fuqaroga tushuntirildi."},
    )

    assert answer.status_code == 303
    db = open_database(world)
    row = db.execute("SELECT status, result FROM callbacks WHERE id = ?", (callback,)).fetchone()
    db.close()
    assert row["status"] == "done" and row["result"] == "Fuqaroga tushuntirildi."
    assert "20.09 09:00" not in client.get("/").text, "a kept promise still shows as open"


def test_the_language_switch_checks_its_token_instead_of_merely_carrying_one(client):
    """The form has always sent a token and the handler ignored it, so another site could
    switch an operator's interface to a language they do not read, mid-shift."""
    sign_in(client)

    forged = client.post("/prefs", data={"language": "ru", "back": "/", "csrf": "nonsense"})
    assert 'lang="uz"' in client.get("/").text, "a forged request changed the language"

    real = client.post("/prefs", data={"language": "ru", "back": "/calls", "csrf": token(client)})
    assert real.headers["location"] == "/calls"
    assert 'lang="ru"' in client.get("/").text

    away = client.post(
        "/prefs", data={"theme": "dark", "back": "//evil.example/", "csrf": token(client)}
    )
    assert away.headers["location"] == "/", "the switcher sent the operator to another host"
    assert forged.status_code == 303


def test_the_search_box_finds_what_it_says_it_finds(client, world):
    """It offers "a question, a number or a ticket" and sent all three to the call
    transcripts — so a ticket number, the most precise thing anyone can type, and the one
    a citizen quotes back over the phone, was the one thing it could never find."""
    from crm import repo

    sign_in(client, "boss")
    db = open_database(world)
    ticket = repo.create_ticket(db, subject="Stipendiya", body="", author_id=1)
    number = repo.ticket(db, ticket)["number"]
    phone = repo.contacts(db, limit=1)[0]["phone"]
    db.close()

    assert client.get(f"/search?q={number}").headers["location"] == f"/tickets/{ticket}"
    assert client.get(f"/search?q={phone}").headers["location"].startswith("/contacts/")
    assert client.get("/search?q=stipendiya").headers["location"] == "/calls?q=stipendiya"
    assert client.get("/search?q=2026-9999").headers["location"] == "/tickets?q=2026-9999"
    assert client.get("/search?q=%20%20").headers["location"] == "/calls"


def test_a_subject_is_a_line_not_an_essay(client):
    """Transcribed questions and pasted notes arrive as one unbroken run of characters.

    Stored whole, a 5000-character subject pushed the card past the edge of the screen and
    took the column beside it with it; in the list it flattened every other row.
    """
    sign_in(client, "boss")

    answer = client.post(
        "/tickets/new",
        data={"csrf": token(client), "subject": "x" * 5000, "body": "text"},
    )

    card = client.get(answer.headers["location"]).text
    assert "x" * 5000 not in card
    assert "x" * 200 in card, "the beginning of the subject should still be there"


def test_a_twenty_digit_row_id_is_answered_not_crashed(client):
    """SQLite refuses to bind anything wider than 64 bits, and that escaped as a bare 500.

    It is reachable by editing the address bar, which is exactly what someone examining
    the system does.
    """
    sign_in(client, "boss")
    huge = "9" * 25

    for path in (f"/calls/{huge}", f"/tickets/{huge}", f"/contacts/{huge}", f"/knowledge/{huge}"):
        answer = client.get(path)
        assert answer.status_code == 404, f"{path} answered {answer.status_code}"


def test_padding_the_login_does_not_open_a_fresh_set_of_guesses(client):
    """The counter used the text as typed while the account was found with it trimmed, so
    " operator" and "operator " were separate counters onto the same account — unlimited
    guessing for anyone who presses the space bar first."""
    from crm.config import settings as crm_settings

    for _ in range(crm_settings.login_attempts + 1):
        client.post(
            "/login", data={"login": "operator", "password": "wrong", "csrf": _token(client)}
        )

    padded = client.post(
        "/login", data={"login": " operator ", "password": PASSWORD, "csrf": _token(client)}
    )
    assert padded.status_code == 200, "a padded login walked straight past the throttle"
    assert client.get("/calls").status_code == 303, "and it must not have created a session"


def _token(client: TestClient) -> str:
    form = client.get("/login")
    marker = 'name="csrf" value="'
    start = form.text.index(marker) + len(marker)
    return form.text[start : form.text.index('"', start)]
