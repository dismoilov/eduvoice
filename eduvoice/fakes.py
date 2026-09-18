"""Stand-ins for the VoiceLab providers.

They let the whole call flow run before person B delivers eduvoice/voicelab/:
same interfaces, no API key, no network. Switch with EDUVOICE_PROVIDER=fake|voicelab.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterable

from eduvoice.audio import tone
from eduvoice.interfaces import (
    SAMPLE_RATE_TTS,
    ChatMessage,
    Language,
    Transcript,
)

CHARS_PER_SECOND = 14  # rough speaking rate, used to fake a realistic audio length


class FakeSpeechToText:
    """Returns prepared texts, one per utterance, then repeats the last one.

    EDUVOICE_FAKE_STT="savol bir|operator bilan gaplashmoqchiman" scripts a whole call,
    which is how the dialogue branches are tested on the server without VoiceLab.
    """

    def __init__(self, texts: Iterable[str] | None = None, latency_ms: int = 300) -> None:
        scripted = [t.strip() for t in os.getenv("EDUVOICE_FAKE_STT", "").split("|") if t.strip()]
        self._texts = list(texts) if texts else scripted or ["stipendiya qanday olinadi"]
        self._index = 0
        self._latency_ms = latency_ms
        self.opened = False

    async def open(self, language: Language) -> None:
        self.opened = True

    async def transcribe(self, pcm16k: bytes) -> Transcript:
        await asyncio.sleep(self._latency_ms / 1000)
        text = self._texts[min(self._index, len(self._texts) - 1)]
        self._index += 1
        return Transcript(
            text=text,
            language="uz",
            duration_ms=int(len(pcm16k) / (16_000 * 2) * 1000),
            latency_ms=self._latency_ms,
        )

    async def close(self) -> None:
        self.opened = False


class FakeTextToSpeech:
    """Streams a tone whose length matches the text, in 100 ms chunks."""

    def __init__(
        self, first_chunk_ms: int = 200, ms_per_char: float = 1000 / CHARS_PER_SECOND
    ) -> None:
        self._first_chunk_ms = first_chunk_ms
        self._ms_per_char = ms_per_char
        self.warmed_up = False
        self.spoken: list[str] = []

    async def warm_up(self, language: Language) -> None:
        self.warmed_up = True

    async def stream(self, text: str, language: Language) -> AsyncIterator[bytes]:
        self.spoken.append(text)
        await asyncio.sleep(self._first_chunk_ms / 1000)
        total_ms = max(100, int(len(text) * self._ms_per_char))
        for _ in range(0, total_ms, 100):
            yield tone(100, SAMPLE_RATE_TTS, frequency=220, amplitude=0.15)
            await asyncio.sleep(0.01)  # generation is faster than real time

    async def close(self) -> None:
        pass


class FakeChatModel:
    """Keyword rules that mimic the decision JSON, so the dialogue can be tested."""

    def __init__(self, latency_ms: int = 400) -> None:
        self._latency_ms = latency_ms

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        await asyncio.sleep(min(self._latency_ms / 1000, timeout_s))
        question = next((m.content for m in reversed(messages) if m.role == "user"), "").lower()

        if "operator" in question:
            return {"intent": "operator"}
        if any(word in question for word in ("xayr", "rahmat", "tugat")):
            return {"intent": "goodbye"}
        if "qaytar" in question or "takrorla" in question:
            return {"intent": "repeat"}
        if "stipendiya" in question:
            return {"intent": "faq", "faq_id": "stipend"}
        if not question.strip():
            return {"intent": "unclear"}
        return {
            "intent": "answer",
            "answer": "Bu savol boʻyicha maʼlumot tayyorlanmoqda.",
        }
