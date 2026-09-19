"""The contract between the bridge and whatever provides speech and a model.

Audio formats are fixed here and must not change silently:

    AudioSocket (Asterisk <-> bridge) : PCM s16le, mono,  8 000 Hz, 20 ms = 320 bytes
    VoiceLab speech-to-text  (input)  : PCM s16le, mono, 16 000 Hz
    VoiceLab text-to-speech  (output) : PCM s16le, mono, 24 000 Hz

Resampling between them is done by the bridge (eduvoice.audio), never by the providers.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

# The service is Uzbek-only (IVR included); the alias stays so adding a language later
# is a one-line change in this file.
Language = Literal["uz"]

SAMPLE_RATE_TELEPHONY = 8_000
SAMPLE_RATE_STT = 16_000
SAMPLE_RATE_TTS = 24_000


@dataclass(frozen=True, slots=True)
class Transcript:
    """Result of one recognised utterance. `text` is empty when no speech was detected."""

    text: str
    language: Language
    duration_ms: int
    latency_ms: int


@dataclass(slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@runtime_checkable
class SpeechToText(Protocol):
    async def open(self, language: Language) -> None:
        """Prepare connections up front, at the start of a call."""

    async def transcribe(self, pcm16k: bytes) -> Transcript:
        """One utterance (0.1-35 s) of PCM s16le mono 16 kHz -> final text.

        Raises SttError on transport or provider failures.
        """

    async def close(self) -> None: ...


@runtime_checkable
class TextToSpeech(Protocol):
    async def warm_up(self, language: Language) -> None:
        """Open connections in advance so the first phrase does not wait for TLS."""

    def stream(self, text: str, language: Language) -> AsyncIterator[bytes]:
        """PCM s16le mono 24 kHz chunks as they are generated.

        Closing the iterator must stop the synthesis (barge-in).
        """

    async def close(self) -> None: ...


@runtime_checkable
class ChatModel(Protocol):
    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        """Model reply parsed as a JSON object (decision schema lives in eduvoice.brain)."""


def describe(exc: BaseException) -> str:
    """The exception as a log line should show it.

    `str(TimeoutError())` is the empty string, so the one line that explains why a caller
    was handed to an operator used to read `turn failed ()` — which is no explanation.
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class ProviderError(Exception):
    """Base class: the bridge treats these as 'provider is unavailable', never as a crash."""


class SttError(ProviderError): ...


class TtsError(ProviderError): ...


class LlmError(ProviderError): ...
