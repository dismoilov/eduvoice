"""VoiceLab: recognition, synthesis and the language model behind the bridge's protocols.

Everything the rest of the bridge knows about this module is `eduvoice/interfaces.py`.
Switching between the fakes and the real service is one setting, `EDUVOICE_PROVIDER`.

What the service gives us, verified against the live API on 18.09.2026:

    POST /v1/stt                 multipart, Idempotency-Key required. On this account it
                                 always answers 202 with a job id, so the transcript is
                                 fetched from /v1/stt/transcriptions/{id}: measured
                                 1.4–2.0 s end to end for a three-second phrase
    POST /v1/tts                 JSON {text, language, voice_id, speed} -> a whole WAV,
                                 mono 16-bit PCM at 24 kHz
    POST /v1/chat/completions    OpenAI-shaped; two generations at a time per account

Three decisions worth knowing:

  * Synthesis is cached on disk by the hash of the text. Service phrases and repeated
    answers then cost nothing and start instantly — the cheapest latency win there is.
  * "No speech detected" is not an error: it returns an empty transcript, and the bridge
    asks the caller to repeat. Raising here would send them to an operator for nothing.
  * Every failure that is not our own becomes SttError / TtsError / LlmError, because
    that is what the bridge turns into "a person will help you" instead of a crash.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import time
import uuid
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from eduvoice.config import Settings
from eduvoice.interfaces import (
    SAMPLE_RATE_STT,
    SAMPLE_RATE_TTS,
    ChatMessage,
    Language,
    LlmError,
    SttError,
    Transcript,
    TtsError,
)

log = logging.getLogger("eduvoice.voicelab")

BASE_URL = "https://api.voicelab.uz"
CHUNK_BYTES = 9600  # 200 ms of 24 kHz PCM: small enough to start playing at once
MODEL_CONCURRENCY = 2  # the account allows two generations at a time
POLL_EVERY_S = 0.2  # how often a queued transcription is asked about


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wraps raw PCM into a WAV container, which is what the upload expects."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def _pcm_from_wav(data: bytes) -> bytes:
    """Takes the samples out of a WAV; a header in the middle of a stream is a click."""
    with wave.open(io.BytesIO(data), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE_TTS:
            log.warning(
                "synthesis returned %d Hz, expected %d", handle.getframerate(), SAMPLE_RATE_TTS
            )
        return handle.readframes(handle.getnframes())


def _nothing_heard() -> Transcript:
    """Silence is not a failure: the bridge asks the caller to repeat, nothing more."""
    log.info("recognition heard no speech")
    return Transcript(text="", language="uz", duration_ms=0, latency_ms=0)


def _describe(response: httpx.Response) -> str:
    """A short, honest reason for the log, with the id their support will ask for."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = body.get("error") or {}
    return (
        f"HTTP {response.status_code} {error.get('code', '')} "
        f"{body.get('message') or error.get('message', '')} "
        f"request_id={body.get('request_id', '?')}"
    ).strip()


class VoiceLabSpeechToText:
    """One utterance in, its text out."""

    def __init__(self, api_key: str, timeout_s: float = 10.0) -> None:
        self._key = api_key
        self._timeout = timeout_s
        self._client: httpx.AsyncClient | None = None

    async def open(self, language: Language) -> None:
        # Opened once per call: the TLS handshake then happens while the greeting plays,
        # not while the caller waits for an answer.
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=self._timeout,
            headers={"Authorization": f"Bearer {self._key}"},
        )

    async def transcribe(self, pcm16k: bytes) -> Transcript:
        if self._client is None:
            await self.open("uz")
        assert self._client is not None
        audio = _wav(pcm16k, SAMPLE_RATE_STT)
        try:
            response = await self._client.post(
                "/v1/stt",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                files={"audio": ("utterance.wav", audio, "audio/wav")},
                data={"language": "uz"},
            )
        except httpx.HTTPError as exc:
            raise SttError(f"could not reach recognition: {exc}") from exc

        started = time.monotonic()
        if response.status_code == 200:  # short audio may come back at once
            return self._as_transcript(response.json(), started)
        if response.status_code == 202:
            return await self._collect(response.json()["id"], started)

        body = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if (body.get("error") or {}).get("code") == "no_speech_detected":
            return _nothing_heard()
        raise SttError(_describe(response))

    async def _collect(self, job_id: str, started: float) -> Transcript:
        """Waits for a queued job, asking about it five times a second."""
        assert self._client is not None
        while time.monotonic() - started < self._timeout:
            await asyncio.sleep(POLL_EVERY_S)
            try:
                answer = await self._client.get(f"/v1/stt/transcriptions/{job_id}")
            except httpx.HTTPError as exc:
                raise SttError(f"could not collect the transcript: {exc}") from exc
            if answer.status_code != 200:
                raise SttError(_describe(answer))
            body = answer.json()
            status = body.get("status")
            if status in ("completed", "done"):
                return self._as_transcript(body, started)
            if status == "failed":
                if (body.get("error") or {}).get("code") == "no_speech_detected":
                    return _nothing_heard()
                raise SttError(body.get("message") or "recognition failed")
        raise SttError(f"recognition did not finish in {self._timeout:.0f} s")

    @staticmethod
    def _as_transcript(body: dict, started: float) -> Transcript:
        return Transcript(
            text=(body.get("transcript") or "").strip(),
            language="uz",
            duration_ms=int(body.get("duration_ms") or 0),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class VoiceLabTextToSpeech:
    """Text in, telephone-ready speech out — with a cache that makes repeats instant."""

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        cache_dir: Path,
        speed: float = 1.0,
        timeout_s: float = 20.0,
    ) -> None:
        self._key = api_key
        self._voice = voice_id
        self._speed = speed
        self._cache = cache_dir
        self._timeout = timeout_s
        self._client: httpx.AsyncClient | None = None

    async def warm_up(self, language: Language) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=self._timeout,
            headers={"Authorization": f"Bearer {self._key}"},
        )

    def _cached_path(self, text: str) -> Path:
        key = hashlib.sha256(f"{text}|{self._voice}|{self._speed}".encode()).hexdigest()[:32]
        return self._cache / f"{key}.pcm"

    async def stream(self, text: str, language: Language) -> AsyncIterator[bytes]:
        text = " ".join(text.split())
        if not text:
            return
        cached = self._cached_path(text)
        if cached.exists():
            pcm = cached.read_bytes()
            for start in range(0, len(pcm), CHUNK_BYTES):
                yield pcm[start : start + CHUNK_BYTES]
            return

        if self._client is None:
            await self.warm_up(language)
        assert self._client is not None
        if not self._voice:
            raise TtsError("no voice is configured (VOICELAB_VOICE_UZ)")
        try:
            response = await self._client.post(
                "/v1/tts",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                json={
                    "text": text[:1000],
                    "language": "uz",
                    "voice_id": self._voice,
                    "speed": self._speed,
                },
            )
        except httpx.HTTPError as exc:
            raise TtsError(f"could not reach synthesis: {exc}") from exc
        if response.status_code != 200:
            raise TtsError(_describe(response))

        pcm = _pcm_from_wav(response.content)
        self._save(cached, pcm)
        for start in range(0, len(pcm), CHUNK_BYTES):
            yield pcm[start : start + CHUNK_BYTES]

    def _save(self, path: Path, pcm: bytes) -> None:
        """Written through a temporary file: a reader never sees half a phrase."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(pcm)
            temporary.replace(path)
        except OSError as exc:  # a read-only disk must not break a call
            log.warning("could not cache synthesis: %s", exc)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class VoiceLabChatModel:
    """The model classifies and drafts; `eduvoice.brain` decides what actually happens."""

    _gate = asyncio.Semaphore(MODEL_CONCURRENCY)

    def __init__(self, api_key: str, model: str = "aisha-comet") -> None:
        self._key = api_key
        self._model = model

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": 400,
            "temperature": 0.2,
        }
        try:
            # The account allows two generations at a time; the gate keeps us inside that.
            async with (
                self._gate,
                httpx.AsyncClient(base_url=BASE_URL, timeout=timeout_s) as client,
            ):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise LlmError(f"could not reach the model: {exc}") from exc
        if response.status_code != 200:
            raise LlmError(_describe(response))

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LlmError(f"unexpected reply shape: {exc}") from exc
        return _as_json(content)


def _as_json(content: str) -> dict:
    """The model is asked for JSON only; some models still wrap it in prose or fences."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LlmError(f"the model did not answer with JSON: {content[:120]!r}")
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise LlmError(f"the model's JSON did not parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LlmError("the model answered with JSON that is not an object")
    return parsed


def build_voicelab_providers(
    settings: Settings,
) -> tuple[VoiceLabSpeechToText, VoiceLabTextToSpeech, VoiceLabChatModel]:
    """What `eduvoice.main` calls when EDUVOICE_PROVIDER=voicelab."""
    if not settings.voicelab_api_key:
        raise RuntimeError("VOICELAB_API_KEY is not set")
    return (
        VoiceLabSpeechToText(settings.voicelab_api_key, timeout_s=settings.stt_timeout_s + 5),
        VoiceLabTextToSpeech(
            settings.voicelab_api_key,
            settings.voicelab_voice_uz,
            cache_dir=settings.audio_dir / "tts-cache",
        ),
        VoiceLabChatModel(settings.voicelab_api_key),
    )
