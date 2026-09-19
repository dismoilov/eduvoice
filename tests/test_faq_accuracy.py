"""How often the assistant gives the right answer — measured, not asserted in prose.

PLAN.md sets the bar: eight of ten test questions must reach the correct decision. This
file is that measurement, run against the answers the project actually ships
(`content/faq.yaml`), so a keyword edited carelessly shows up here rather than on the
phone.

The questions are the ones a ministry call centre really gets, including the two hard
cases: a question that contains the words of a *more general* answer, and one that is
none of our business.
"""

import asyncio
from pathlib import Path

import pytest

from eduvoice.brain import Brain
from eduvoice.content import Faq
from eduvoice.fakes import FakeChatModel
from eduvoice.interfaces import ChatMessage

CONTENT = Path(__file__).resolve().parent.parent / "content"

# question -> the faq_id it must reach, or the action when no answer applies
QUESTIONS: list[tuple[str, str]] = [
    ("Stipendiya qanday olinadi?", "stipend"),
    ("Stipendiyani qachon toʻlashadi?", "stipend"),
    ("Akademik taʼtil qanday rasmiylashtiriladi?", "academic_leave"),
    # Contains "stipendiya", so the general stipend answer also matches — and used to win.
    ("Akademik taʼtilda stipendiya toʻlanadimi?", "stipend_academic_leave"),
    ("Akademik taʼtil qancha muddatga beriladi?", "academic_leave_duration"),
    ("Boshqa universitetga koʻchsam boʻladimi?", "transfer_university"),
    ("Oʻqishni koʻchirish uchun qanday hujjat kerak?", "transfer_documents"),
    ("Operator bilan gaplashmoqchiman", "action:transfer"),
    ("Rahmat, xayr", "action:goodbye"),
    ("Bugun ob-havo qanday?", "action:clarify"),  # not our subject
]


class ModelThatKnowsItsLimits(FakeChatModel):
    """The standing fake, with one correction: it declines subjects that are not ours.

    The plain fake drafts an answer for anything at all, which would flatter this
    measurement — the real model is instructed to reply `out_of_scope` off-subject, and
    the point of the tenth question is to check that a caller asking about the weather is
    not read a regulation.
    """

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        answer = await super().complete_json(messages, timeout_s)
        return {"intent": "out_of_scope"} if answer.get("intent") == "answer" else answer


def decide(question: str) -> str:
    brain = Brain(ModelThatKnowsItsLimits(), faq=Faq.load(CONTENT), timeout_s=1.0)
    decision = asyncio.run(brain.decide(question, turn_index=0))
    return decision.faq_id or f"action:{decision.action}"


@pytest.mark.parametrize("question, expected", QUESTIONS, ids=[q for q, _ in QUESTIONS])
def test_each_question_reaches_the_right_answer(question, expected):
    assert decide(question) == expected


def test_the_whole_set_clears_the_bar_set_in_the_plan():
    """Eight of ten, which is the criterion PLAN.md section 1 was written against."""
    correct = sum(1 for question, expected in QUESTIONS if decide(question) == expected)
    assert correct >= 8, f"only {correct} of {len(QUESTIONS)} questions were answered correctly"


def test_a_more_specific_keyword_beats_a_more_general_one():
    """The rule behind the measurement, stated on its own so it cannot regress quietly.

    A general answer is always written before a particular one, so first-match handed the
    caller the wrong regulation: asked whether a stipend is paid *during academic leave*,
    the assistant read out the general rule about stipends.
    """
    faq = Faq.load(CONTENT)

    general = faq.match("stipendiya qanday olinadi")
    particular = faq.match("akademik taʼtilda stipendiya toʻlanadimi")

    assert general is not None and particular is not None
    assert general.faq_id == "stipend"
    assert particular.faq_id == "stipend_academic_leave"
    assert "toʻlanmaydi" in particular.answer, "the specific answer says the opposite thing"


# What each answer must say, in the caller's own terms. Six lines standing between a
# careless edit and a citizen being told the opposite of the law: only one of these was
# pinned before, and reversing "toʻlanmaydi" to "toʻlanadi" — no stipend during academic
# leave, into: there is one — changed the legal meaning with nothing to stop it.
MUST_SAY: list[tuple[str, tuple[str, ...], str]] = [
    ("stipend", ("toʻlanadi",), "59-son"),
    ("stipend_academic_leave", ("toʻlanmaydi",), "344-son"),
    ("academic_leave", ("ariza",), "344-son"),
    ("academic_leave_duration", ("semestr",), "344-son"),
    ("transfer_university", ("koʻchirish",), "578-son"),
    ("transfer_documents", ("ariza", "maʼlumotnoma"), "578-son"),
]


@pytest.mark.parametrize("faq_id, words, document", MUST_SAY, ids=[m[0] for m in MUST_SAY])
def test_each_answer_still_says_what_the_regulation_says(faq_id, words, document):
    faq = Faq.load(CONTENT)
    entry = faq.entry(faq_id)

    assert entry is not None, f"{faq_id} is gone from content/faq.yaml"
    for word in words:
        assert word in entry.answer, f"{faq_id} no longer says {word!r}"
    assert document in entry.source, f"{faq_id} no longer cites {document}"
