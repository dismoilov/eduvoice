"""Answering from the regulations themselves.

The danger here is not that the search finds nothing — that just hands the caller to a
person, as before. It is that it finds *something* for every question and the model
dresses it up as an answer. A ministry call centre that confidently misquotes a decree is
worse than one that says "let me put you through". Most of these tests are about the
second kind of failure.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest

from eduvoice.brain import Brain
from eduvoice.content import Faq, FaqEntry
from eduvoice.interfaces import ChatMessage
from store.db import open_database
from store.lex import Clause, LexLibrary, parse_document, save_document, search

# The shape lex.uz actually serves: every element of an act is a <div name="ID" id="ID">,
# wrapped in a toolbar div that carries the same words on every clause. Shortened, but
# the structure and the Uzbek spelling (oʻ/gʻ with U+02BB) are exactly as published.
PAGE = """
<html><head><title>&nbsp;344-сон 03.06.2021.&nbsp;Akademik taʼtil berish
toʻgʻrisidagi nizomni tasdiqlash haqida</title></head><body>
<div class="ACT_TEXT lx_elem"><div class="lx_elem2"><div class="lx_elem3">
<span>Hujjatga taklif yuborish</span></div></div>
<div name="-100" id="-100">1-bob. Umumiy qoidalar</div></div>
<div class="ACT_TEXT lx_elem"><div class="lx_elem2"><div class="lx_elem3">
<span>Hujjatga taklif yuborish</span></div></div>
<div name="-101" id="-101">2. Talabalarga akademik taʼtil quyidagi hollarda
berilishi mumkin:</div></div>
<div class="ACT_TEXT lx_elem"><div name="-102" id="-102">oilasining betob aʼzosini
parvarish qilish uchun, tibbiy xulosa asosida beriladi;</div></div>
<div class="ACT_TEXT lx_elem"><div name="-103" id="-103">5. Talabalarga akademik
taʼtil davrida stipendiyalar toʻlanmaydi.</div></div>
<div class="ACT_TEXT lx_elem"><div name="-104" id="-104">7. Chet elga oʻqishga
yuborilgan talabalarga akademik taʼtil berilmaydi va qayta tiklanadi.</div></div>
<div class="ACT_TEXT lx_elem"><div name="-105" id="-105">8. Akademik taʼtil muddati
bir yilgacha davom etadi va uzaytirilishi mumkin emas.</div></div>
<div class="ACT_TEXT lx_elem"><div name="-106" id="-106">9. Taʼlim jarayoni oʻquv
yili davomida davom etadi va dekan buyrugʻi bilan rasmiylashtiriladi.</div></div>
<div class="ACT_TEXT lx_elem"><div name="-107" id="-107">10. Kontrakt toʻlovi
boʻlib toʻlanadi va muddati shartnomada davom etadi.</div></div>
<div class="ACT_TEXT lx_elem"><div name="-108" id="-108">11. Talaba arizasi dekanat
tomonidan koʻrib chiqiladi va javob beriladi.</div></div>
<div class="ACT_TEXT lx_elem"><div name="-109" id="-109">12. Talabalar turar joyiga
joylashish tartibi alohida hujjat bilan belgilanadi.</div></div>
</body></html>
"""


def corpus() -> sqlite3.Connection:
    db = open_database(Path(tempfile.mkdtemp()) / "lex.db")
    save_document(db, parse_document(PAGE, "-5443081"))
    return db


class Recording:
    """A model that answers from the law, and remembers what it was asked."""

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.calls: list[list[ChatMessage]] = []

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        self.calls.append(messages)
        if len(self.calls) == 1:  # the first call is the classifier
            return {"intent": "unclear"}
        return self.reply


def test_the_clauses_of_a_decree_are_read_with_their_anchors():
    """Each clause keeps lex.uz's own element id, which is what makes a citation checkable:
    the link opens the decree at that clause, not at the top of ninety pages."""
    document = parse_document(PAGE, "-5443081")

    assert document.number == "344-son"
    assert document.adopted_at == "03.06.2021"
    texts = [text for _element, _band, text in document.clauses]
    assert any("stipendiyalar toʻlanmaydi" in text for text in texts)
    assert all("Hujjatga taklif yuborish" not in text for text in texts), "toolbar text indexed"
    assert "1-bob. Umumiy qoidalar" not in texts, "a chapter heading is not a rule"

    numbered = {band: element for element, band, _text in document.clauses}
    assert numbered["5"] == "-103", "the clause lost the anchor it is cited by"


def test_a_question_about_something_else_entirely_finds_nothing():
    """The whole safety of this feature. The query is an OR of the question's words, so
    the search ranks *something* first for almost any question; if that were handed to
    the model, a caller asking how long a football match lasts would be read the clause
    on how long academic leave lasts — both "davom etadi" — and told it was the law.

    The first two questions below do reach the index: they share "davom etadi" with three
    clauses of the fixture. They are rejected on the words they share, not for want of a
    match.
    """
    db = corpus()

    for question in [
        "Futbol o'yini necha daqiqa davom etadi?",
        "Bu ish qancha davom etadi?",
        "Avtobus qachon keladi?",
        "Pitsa buyurtma qilmoqchiman",
        "Salom",
    ]:
        assert search(db, question) == [], f"{question!r} was answered from the law"


def test_a_question_made_only_of_common_words_finds_nothing():
    """ "Davom etadi" — "lasts" — is in three clauses of this fixture, and in a real
    decree it is everywhere. Two such words satisfy any count-based floor, so the rule is
    not how many words match but whether any of them says what the question is about.
    """
    db = corpus()
    matched = db.execute(
        "SELECT count(*) FROM lex_fts WHERE lex_fts MATCH 'davom* OR etadi*'"
    ).fetchone()[0]
    assert matched >= 3, "the fixture no longer exercises the common-word case"

    assert search(db, "Bu ish qancha davom etadi?") == []
    assert search(db, "Bu qanday beriladi, qanday qilib mumkin?") == []


def test_the_clauses_that_only_share_grammar_are_left_out_of_a_real_answer():
    """The dangerous case is not a question that finds nothing — it is a good question
    whose neighbours come along for the ride.

    "Kontrakt toʻlovi qancha davom etadi?" genuinely matches the clause about contract
    payment. It also matches two clauses that merely contain "davom etadi", one of them
    about academic leave. Handing all three to the model invites it to answer about the
    wrong one, and every extract it is given is one more thing it may quote.
    """
    db = corpus()

    found = search(db, "Kontrakt to'lovi qancha davom etadi?")

    assert [clause.band for clause in found] == ["10"], "clauses matching only grammar came too"


def test_one_matching_word_out_of_several_is_not_enough():
    """A single word in common is a coincidence, not a subject. The clause on contract
    payment shares exactly one word with this question and answers nothing in it."""
    db = corpus()

    assert search(db, "Kontrakt va avtobus haqida?") == []


def test_the_caller_gets_the_clause_that_answers_them_whatever_ending_they_used():
    """The decree says "taʼtil" and "stipendiyalar"; the caller says "taʼtilda" and
    "stipendiya", and the recogniser writes the apostrophe its own way. Uzbek glues its
    endings on, so matching whole words finds nothing at all."""
    db = corpus()

    found = search(db, "Akademik ta'tilda stipendiya to'lanadimi?")

    assert found, "the answer is in the decree but was not found"
    assert "toʻlanmaydi" in found[0].text
    assert found[0].citation == "344-son, 5-band"
    assert found[0].link == "https://lex.uz/uz/docs/-5443081#-103"


def test_a_clause_that_is_only_the_opening_of_a_list_is_given_with_its_items():
    """ "Academic leave is granted in the following cases:" answers nothing by itself, and
    the model refuses it — correctly. The cases are separate elements of the act, so
    without them a whole rule is invisible."""
    db = corpus()

    found = search(db, "Akademik ta'til qanday hollarda beriladi?")

    assert found, "the rule was not found at all"
    assert "parvarish qilish uchun" in found[0].text, "the list items were left behind"


def test_fetching_a_decree_again_replaces_it_instead_of_doubling_it():
    """An amended decree must not sit in the index beside its own earlier wording: the
    assistant would be able to quote a clause that no longer has force."""
    db = corpus()
    before = db.execute("SELECT count(*) FROM lex_clauses").fetchone()[0]

    save_document(db, parse_document(PAGE, "-5443081"))

    after = db.execute("SELECT count(*) FROM lex_clauses").fetchone()[0]
    assert after == before, "the decree was indexed twice"
    assert db.execute("SELECT count(*) FROM lex_documents").fetchone()[0] == 1


def test_a_missing_database_is_not_a_failed_call():
    """The corpus is optional. A bridge started before `make lex-fetch` has ever run must
    answer exactly as it did before, not drop the caller."""
    library = LexLibrary(Path(tempfile.mkdtemp()) / "not-created-yet.db")

    assert library.clauses() == 0
    assert library.find("Akademik ta'til qanday olinadi?") == []


class Fixed:
    """A law source that always finds the same clause."""

    def __init__(self, *clauses: Clause) -> None:
        self._clauses = list(clauses)

    def find(self, question: str) -> list[Clause]:
        return self._clauses


CLAUSE = Clause(
    number="344-son",
    title="Akademik taʼtil",
    doc_id="-5443081",
    element="-103",
    band="5",
    text="5. Talabalarga akademik taʼtil davrida stipendiyalar toʻlanmaydi.",
)


async def test_the_law_is_read_only_when_nothing_else_answered():
    """Every call would otherwise pay for a second model call on every turn, and the
    keyword path — the one that answers in under a second — would stop being fast."""
    model = Recording({"clause": 1, "answer": "Yoʼq, toʻlanmaydi."})
    answers = Faq(
        {"stipend": FaqEntry("stipend", "Stipendiya oyiga bir marta toʻlanadi.", ["stipendiya"])}
    )
    brain = Brain(model, faq=answers, laws=Fixed(CLAUSE))

    decision = await brain.decide("Stipendiya qanday olinadi?")

    assert decision.action == "faq"
    assert model.calls == [], "the model was called although a keyword answered"


async def test_an_answer_from_the_law_carries_the_clause_it_came_from():
    """Task three in one test: what the caller hears has to be traceable to a published
    document, down to the clause, or it is just a chatbot with a telephone number."""
    model = Recording({"clause": 1, "answer": "Akademik taʼtil davrida stipendiya toʻlanmaydi."})
    brain = Brain(model, faq={}, laws=Fixed(CLAUSE))

    decision = await brain.decide("Akademik ta'tilda stipendiya to'lanadimi?")

    assert decision.action == "answer"
    assert decision.intent == "law"
    assert "344-son, 5-band" in decision.source
    assert "lex.uz/uz/docs/-5443081#-103" in decision.source, "the citation cannot be checked"
    assert len(model.calls) == 2, "the law was not consulted"
    assert CLAUSE.text in model.calls[1][1].content, "the model was not given the clause"


@pytest.mark.parametrize(
    "reply",
    [
        {"clause": 0, "answer": ""},  # the extracts do not answer it
        {"clause": 0, "answer": "Ha, toʻlanadi."},  # answered without naming a clause
        {"clause": 7, "answer": "Ha, toʻlanadi."},  # named a clause it was never given
        {"clause": 1, "answer": "   "},  # nothing to say
        {"answer": "Ha, toʻlanadi."},  # no clause field at all
    ],
)
async def test_an_answer_the_model_cannot_pin_to_a_clause_is_thrown_away(reply):
    """A model that answers without pointing at the text in front of it is answering from
    memory, which is the one thing this whole path exists to prevent. Better the caller
    hears "I did not understand you" and gets a person."""
    brain = Brain(Recording(reply), faq={}, laws=Fixed(CLAUSE))

    decision = await brain.decide("Akademik ta'tilda stipendiya to'lanadimi?")

    assert decision.action != "answer", f"{reply} reached the caller"
    assert decision.intent != "law"


async def test_a_broken_law_search_does_not_break_the_call():
    """The corpus is a convenience. Whatever happens in it, the caller must end up where
    they would have ended up without it."""

    class Exploding:
        def find(self, question: str):
            raise RuntimeError("the index is corrupt")

    brain = Brain(Recording({"clause": 1, "answer": "x"}), faq={}, laws=Exploding())

    with pytest.raises(RuntimeError):
        # The contract is that a law source never raises; LexLibrary honours it, and this
        # records that Brain relies on it rather than silently swallowing every error.
        await brain.decide("Akademik ta'til qanday olinadi?")


class SlowReader:
    """Classifies at once, but takes its time over the extracts — as the real model does
    on a slow evening: three clauses of legal text are more to read than one line."""

    def __init__(self, read_for_s: float) -> None:
        self._read_for_s = read_for_s
        self.calls = 0

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {"intent": "unclear"}
        await asyncio.sleep(self._read_for_s)
        return {"clause": 1, "answer": "Toʻlov miqdorini oliy taʼlim muassasasi belgilaydi."}


async def test_reading_the_law_gets_its_own_time():
    """Seen live: the classifier's five seconds were applied to the reading too, the
    reading took six, and a caller asking about a hall of residence was told the
    assistant only answers questions about education. The wait is spoken through, so
    the reading may take longer than the classification."""
    brain = Brain(
        SlowReader(read_for_s=0.3), faq={}, timeout_s=0.1, laws=Fixed(CLAUSE), law_timeout_s=2.0
    )

    decision = await brain.decide("Talabalar turar joyi uchun toʻlov qancha?")

    assert decision.intent == "law", "the reading was cut off by the classifier's budget"
    assert "344-son" in decision.source


class Memory:
    """Answers the question from memory first — wrongly — and reads the law correctly
    when it is put in front of it. The real model did exactly this on 19.09."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {"intent": "answer", "answer": "Bakalavriat toʻrt yil davom etadi."}
        return {"clause": 1, "answer": "Bakalavriat kamida uch yil davom etadi."}


DURATION = Clause(
    number="3807-son",
    title="Oliy taʼlim toʻgʻrisidagi nizom",
    doc_id="-8117474",
    element="-8120411",
    band="13",
    text="13. Bakalavriat taʼlim bosqichida oʻqish kamida uch yil davom etadi.",
)


async def test_an_answer_from_memory_is_replaced_by_the_clause_that_actually_says_it():
    """Asked how long a bachelor's degree takes, the live model said "usually four years"
    — from memory, with no source. The regulation says at least three. On a ministry
    line the clause is read out, with its number, and the model's own version is not."""
    model = Memory()
    brain = Brain(model, faq={}, laws=Fixed(DURATION))

    decision = await brain.decide("Bakalavriat necha yil davom etadi?")

    assert decision.intent == "law", "the answer from memory reached the caller"
    assert "uch yil" in decision.text and "toʻrt" not in decision.text
    assert "3807-son, 13-band" in decision.source
    assert model.calls == 2, "the law was not consulted"


async def test_an_answer_from_memory_with_nothing_in_the_law_is_not_read_out():
    """The other case: the model has an answer and the regulations have nothing on it.
    Nothing is what the caller gets from the assistant — a person, not a guess."""
    brain = Brain(Memory(), faq={}, laws=Fixed())

    decision = await brain.decide("Bakalavriat necha yil davom etadi?")

    assert decision.action != "answer", "an unsourced answer was read out"
    assert decision.source == ""


async def test_without_a_law_corpus_the_model_may_still_answer():
    """Fake mode and the tests run without regulations. There the model's own answer is
    the only one there is, and it is kept — the guard exists for the corpus, not
    instead of it."""
    brain = Brain(Memory(), faq={}, laws=None)

    decision = await brain.decide("Bakalavriat necha yil davom etadi?")

    assert decision.action == "answer"
    assert "toʻrt yil" in decision.text
