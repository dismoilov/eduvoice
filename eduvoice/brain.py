"""Turns a recognised question into a decision.

The language model only classifies and drafts wording; what actually happens to the call
is decided by the rules below. A model that invents an intent cannot transfer, hang up
or answer something outside the FAQ.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from eduvoice.content import Faq
from eduvoice.interfaces import ChatMessage, ChatModel, LlmError, describe

log = logging.getLogger("eduvoice.brain")

Action = Literal["answer", "faq", "clarify", "repeat", "transfer", "goodbye"]


class LawClause(Protocol):
    """One clause of one regulation, as the local index returns it."""

    @property
    def citation(self) -> str: ...
    @property
    def link(self) -> str: ...
    @property
    def text(self) -> str: ...


class LawSource(Protocol):
    """Whatever can find the clauses that bear on a question. Must never raise."""

    def find(self, question: str) -> Sequence[LawClause]: ...


SYSTEM_PROMPT = """You are the voice assistant of the Ministry of Higher Education of Uzbekistan.
You answer only questions about higher education and only in Uzbek (Latin script).

Return JSON only, no other text:
{"intent": "faq|answer|operator|repeat|goodbye|unclear|out_of_scope",
 "faq_id": "<id from the list, only with intent=faq>",
 "answer": "<2-3 short sentences for speech, only with intent=answer>"}

Rules:
- Prefer an FAQ entry when the question matches one.
- If you are not sure of the answer, use intent=unclear. Never invent rules or numbers.
- Any request to speak to a person means intent=operator.
- Students' everyday matters are higher education too: stipends, contracts, halls of
  residence, rent, academic leave, transfers, admission, exams, diplomas.
- Write numbers and dates as words, do not use lists, brackets or links.
"""

# Used only when the FAQ has nothing and the model itself was unsure. The extracts are
# the law as lex.uz publishes it; the model's job here is to read, not to know. It is
# given no room to answer from memory: either the text in front of it answers the
# question, or it says so and a person takes the call.
LAW_PROMPT = """You are the voice assistant of the Ministry of Higher Education of Uzbekistan.

Below are numbered extracts from the current regulations of Uzbekistan. Answer the
caller's question ONLY from these extracts.

Return JSON only, no other text:
{"clause": <the number of the extract you used>,
 "answer": "<2-3 short sentences in Uzbek, Latin script, to be read aloud>"}

If the extracts do not answer the question, return exactly:
{"clause": 0, "answer": ""}

Rules:
- Never state a rule, figure, deadline or amount that is not written in the extracts.
- An extract about a neighbouring subject is not an answer. When in doubt, return zero.
- Do not quote the extract: say what it means, in plain spoken Uzbek.
- Write numbers and dates as words, do not use lists, brackets or links.
"""


@dataclass(slots=True)
class Decision:
    action: Action
    intent: str
    text: str = ""
    faq_id: str | None = None
    latency_ms: float = 0.0
    # Which document the answer comes from; empty when the text was written by the model.
    source: str = ""


class Brain:
    def __init__(
        self,
        model: ChatModel,
        faq: Faq | dict[str, str] | None = None,
        timeout_s: float = 5.0,
        max_turns: int = 8,
        laws: LawSource | None = None,
        law_timeout_s: float | None = None,
    ) -> None:
        self._model = model
        if isinstance(faq, dict):
            faq = Faq.from_answers(faq)
        self._faq_source = faq or Faq({})
        self._faq = self._faq_source.answers()
        self._timeout_s = timeout_s
        self._max_turns = max_turns
        self._laws = laws
        # Reading three extracts takes the model longer than classifying one line, and
        # this call is the last thing between the caller and "I did not understand".
        self._law_timeout_s = law_timeout_s if law_timeout_s is not None else timeout_s
        self.unclear_streak = 0

    async def close(self) -> None:
        """Releases whatever the model holds open.

        The providers are built per call, so a model that keeps a connection — as the real
        one now does, to save a TLS handshake on every turn — would otherwise leak one
        socket per caller.
        """
        closer = getattr(self._model, "close", None)
        if closer is not None:
            await closer()

    def _faq_catalogue(self) -> str:
        if not self._faq:
            return "FAQ list is empty."
        return "FAQ ids:\n" + "\n".join(f"- {faq_id}" for faq_id in self._faq)

    async def decide(self, question: str, turn_index: int = 0) -> Decision:
        """One question -> one decision. Never raises: failures become a transfer."""
        if not question.strip():
            return self._unclear("empty")

        if turn_index >= self._max_turns:
            return Decision(action="transfer", intent="limit_reached")

        # Fast path: an obvious keyword match skips the model entirely (about a second
        # saved). An answer with no text in it is not taken: the caller would hear pure
        # silence and, hearing nothing wrong, would wait through it and then be hung up
        # on. A person is the right answer to an answer we do not have.
        shortcut = self._faq_source.match(question)
        if shortcut is not None and shortcut.answer.strip():
            self.unclear_streak = 0
            return Decision(
                action="faq",
                intent="faq_keyword",
                text=shortcut.answer,
                faq_id=shortcut.faq_id,
                source=shortcut.source,
            )
        if shortcut is not None:
            log.warning("faq %s is published with an empty answer -> operator", shortcut.faq_id)
            return Decision(action="transfer", intent="empty_answer")

        messages = [
            ChatMessage("system", SYSTEM_PROMPT + "\n" + self._faq_catalogue()),
            ChatMessage("user", question),
        ]
        started = asyncio.get_running_loop().time()
        try:
            raw = await asyncio.wait_for(
                self._model.complete_json(messages, timeout_s=self._timeout_s),
                timeout=self._timeout_s,
            )
        except (LlmError, TimeoutError) as exc:
            log.warning("model failed (%s) -> transfer to operator", describe(exc))
            return Decision(action="transfer", intent="tech_problem")
        latency_ms = (asyncio.get_running_loop().time() - started) * 1000

        intent = str(raw.get("intent", "unclear"))
        decision = self._from_intent(intent, raw)
        decision.latency_ms = latency_ms

        if decision.action == "clarify":
            # Last resort before "I did not understand you": the regulations themselves.
            # Only here, so the common paths cost nothing extra, and only from the text
            # in front of the model — see `_from_law`.
            if (from_law := await self._from_law(question)) is not None:
                self.unclear_streak = 0
                from_law.latency_ms = (
                    asyncio.get_running_loop().time() - started
                ) * 1000  # both model calls
                return from_law
            self.unclear_streak += 1
            if self.unclear_streak >= 2:
                return Decision(action="transfer", intent="unclear_twice", latency_ms=latency_ms)
        else:
            self.unclear_streak = 0
        return decision

    async def _from_law(self, question: str) -> Decision | None:
        """Answers from the regulations, or returns None and lets the caller be handed on.

        Two things have to agree before a word of this reaches the caller: the local
        search has to find clauses that share the question's distinctive words, and the
        model has to point at the one it used. A model that answers without naming a
        clause, or names one it was not given, is answering from memory — which is
        exactly what task three forbids — so that answer is thrown away.
        """
        if self._laws is None:
            return None
        clauses = self._laws.find(question)
        if not clauses:
            return None

        extracts = "\n\n".join(
            f"[{index}] {clause.citation}: {clause.text}"
            for index, clause in enumerate(clauses, start=1)
        )
        messages = [
            ChatMessage("system", LAW_PROMPT),
            ChatMessage("user", f"{extracts}\n\nSavol: {question}"),
        ]
        try:
            raw = await asyncio.wait_for(
                self._model.complete_json(messages, timeout_s=self._law_timeout_s),
                timeout=self._law_timeout_s,
            )
        except (LlmError, TimeoutError) as exc:
            # Not a transfer: the caller still has the ordinary "I did not understand"
            # path, and that is a better outcome than a technical failure message.
            log.warning("law answer failed (%s)", describe(exc))
            return None

        text = str(raw.get("answer", "")).strip()
        try:
            chosen = int(raw.get("clause", 0))
        except (TypeError, ValueError):
            chosen = 0
        if not text or not 1 <= chosen <= len(clauses):
            log.info("the law does not answer %r (clause=%r)", question[:60], raw.get("clause"))
            return None

        clause = clauses[chosen - 1]
        log.info("answered from %s for %r", clause.citation, question[:60])
        return Decision(
            action="answer",
            intent="law",
            text=text,
            source=f"{clause.citation} · {clause.link}",
        )

    def _from_intent(self, intent: str, raw: dict) -> Decision:
        if intent == "operator":
            return Decision(action="transfer", intent=intent)
        if intent == "goodbye":
            return Decision(action="goodbye", intent=intent)
        if intent == "repeat":
            return Decision(action="repeat", intent=intent)
        if intent == "faq":
            faq_id = str(raw.get("faq_id", ""))
            if faq_id in self._faq:
                entry = self._faq_source.entry(faq_id)
                return Decision(
                    action="faq",
                    intent=intent,
                    faq_id=faq_id,
                    text=self._faq[faq_id],
                    source=entry.source if entry else "",
                )
            log.warning("model returned unknown faq_id %r", faq_id)
            return self._unclear(intent)
        if intent == "answer":
            text = str(raw.get("answer", "")).strip()
            if text:
                return Decision(action="answer", intent=intent, text=text)
            return self._unclear(intent)
        if intent == "out_of_scope":
            return Decision(action="clarify", intent=intent)
        return self._unclear(intent)

    @staticmethod
    def _unclear(intent: str) -> Decision:
        return Decision(action="clarify", intent=intent or "unclear")
