"""The safety rules around the language model.

The model may only classify and draft wording. These tests pin down the part that
matters for a public service: a model that hallucinates cannot invent an answer, and
anything it gets wrong ends with a human, not with a wrong statement about the law.
"""

from eduvoice.brain import Brain
from eduvoice.content import Faq, FaqEntry
from eduvoice.interfaces import ChatMessage, LlmError

FAQ = {"stipend": "Stipendiya javobi."}


class ScriptedModel:
    """Replies with whatever the test prepared, one reply per question."""

    def __init__(self, *replies: dict) -> None:
        self._replies = list(replies)
        self.calls = 0

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        self.calls += 1
        return self._replies[min(self.calls - 1, len(self._replies) - 1)]


class FailingModel:
    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        raise LlmError("provider is down")


async def test_a_keyword_match_answers_without_calling_the_model():
    """The fast path: about a second of delay saved on the most common questions."""
    model = ScriptedModel({"intent": "answer", "answer": "must not be used"})
    faq = Faq({"stipend": FaqEntry("stipend", FAQ["stipend"], keywords=["stipendiya"])})
    brain = Brain(model, faq=faq)

    decision = await brain.decide("stipendiya qanday olinadi")

    assert model.calls == 0, "the model was called although a keyword matched"
    assert decision.action == "faq"
    assert decision.text == FAQ["stipend"]


async def test_an_unknown_faq_id_is_never_spoken():
    """A hallucinated id must not turn into a confident answer."""
    brain = Brain(ScriptedModel({"intent": "faq", "faq_id": "there_is_no_such_id"}), faq=FAQ)

    decision = await brain.decide("nimadir soʻradim")

    assert decision.action == "clarify"
    assert decision.text == ""


async def test_an_empty_answer_is_never_spoken():
    brain = Brain(ScriptedModel({"intent": "answer", "answer": "   "}), faq=FAQ)

    assert (await brain.decide("nimadir soʻradim")).action == "clarify"


async def test_two_misunderstandings_in_a_row_go_to_a_human():
    brain = Brain(ScriptedModel({"intent": "unclear"}), faq=FAQ)

    assert (await brain.decide("birinchi savol")).action == "clarify"
    second = await brain.decide("ikkinchi savol")

    assert second.action == "transfer"
    assert second.intent == "unclear_twice"


async def test_a_long_conversation_goes_to_a_human():
    """Nobody should argue with a robot forever."""
    brain = Brain(ScriptedModel({"intent": "answer", "answer": "javob"}), faq=FAQ, max_turns=3)

    decision = await brain.decide("savol", turn_index=3)

    assert decision.action == "transfer"
    assert decision.intent == "limit_reached"


async def test_a_broken_model_transfers_to_a_human():
    brain = Brain(FailingModel(), faq=FAQ)

    decision = await brain.decide("savol")

    assert decision.action == "transfer"
    assert decision.intent == "tech_problem"


async def test_the_model_never_decides_to_hang_up_by_inventing_an_intent():
    brain = Brain(ScriptedModel({"intent": "hangup_the_call_now"}), faq=FAQ)

    assert (await brain.decide("savol")).action == "clarify"


def test_an_apostrophe_never_breaks_the_fast_path():
    """Recognition returns ta'til, the answer is filed under taʼtil: still one word."""
    faq = Faq({"leave": FaqEntry("leave", "Akademik taʼtil javobi", keywords=["akademik taʼtil"])})

    assert faq.match("Akademik ta'til qanday rasmiylashtiriladi") is not None
    assert faq.match("akademik ta`til") is not None
    assert faq.match("AKADEMIK TAʼTIL") is not None


def test_keywords_from_the_file_survive_the_apostrophe_the_recogniser_uses(tmp_path):
    """Recognition returns `ta'til`; the answers are written `taʼtil`. One letter, two codes.

    The database loader folded them and the file loader did not, so five of the shipped
    keywords — every one containing `oʻ`, `gʻ` or `ʼ` — could never match. The file is the
    fallback used when the CRM has no answers yet, which is exactly the path nobody tries
    by hand.
    """
    from eduvoice.content import Faq

    (tmp_path / "faq.yaml").write_text(
        "transfer:\n  keywords: [koʻchirish]\n  answer: Javob.\n", encoding="utf-8"
    )
    faq = Faq.load(tmp_path)

    for spelling in ("koʻchirish haqida", "ko'chirish haqida", "KOʻCHIRISH"):
        assert faq.match(spelling) is not None, f"{spelling!r} did not reach the answer"


async def test_an_answer_published_with_no_text_sends_the_caller_to_a_person():
    """A supervisor clearing the answer field is one keystroke away at any moment.

    The keyword shortcut used to take it, and `_speak("")` writes nothing at all — so the
    caller heard the greeting, asked their question, and got pure silence. Hearing nothing
    wrong they would ask again, get silence again, and be hung up on without ever reaching
    a human. Silence is the one thing this system must never do.
    """
    from eduvoice.content import Faq, FaqEntry

    faq = Faq({"stipend": FaqEntry(faq_id="stipend", answer="   ", keywords=["stipendiya"])})
    from eduvoice.fakes import FakeChatModel

    brain = Brain(FakeChatModel(), faq=faq, timeout_s=1.0)

    decision = await brain.decide("stipendiya qanday olinadi", turn_index=0)

    assert decision.action == "transfer"
    assert decision.intent == "empty_answer"
