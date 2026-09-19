"""Service phrases and their audio.

Phrases are spoken on every call, so they are synthesised once and cached as raw 8 kHz
PCM (audio/prompts/<id>.pcm). A cache hit removes the text-to-speech delay completely,
which is the cheapest latency win we have — and the reason the greeting starts instantly.

The cache is filled at start-up (`prewarm`) and, if something was missed, on first use.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

import yaml

from eduvoice.audio import resample
from eduvoice.interfaces import SAMPLE_RATE_TELEPHONY, SAMPLE_RATE_TTS, Language, TextToSpeech

log = logging.getLogger("eduvoice.prompts")


class PromptLibrary:
    """Texts from content/prompts.yaml plus their cached audio."""

    def __init__(
        self, texts: dict[str, str], cache_dir: Path | None = None, voice: str = ""
    ) -> None:
        self._texts = texts
        self._cache_dir = cache_dir
        self._voice = voice
        self._audio: dict[str, bytes] = {}
        # One lock per phrase: two calls starting at the same second must not synthesise
        # the same greeting twice.
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def load(
        cls, content_dir: Path, audio_dir: Path | None = None, voice: str = ""
    ) -> PromptLibrary:
        path = content_dir / "prompts.yaml"
        texts = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cache = audio_dir / "prompts" if audio_dir else None
        return cls({k: str(v).strip() for k, v in texts.items()}, cache, voice)

    def ids(self) -> list[str]:
        return list(self._texts)

    def text(self, prompt_id: str) -> str:
        try:
            return self._texts[prompt_id]
        except KeyError:
            raise KeyError(f"prompt '{prompt_id}' is missing from prompts.yaml") from None

    async def audio(self, prompt_id: str, tts: TextToSpeech, language: Language = "uz") -> bytes:
        """8 kHz PCM for the phrase: from memory, from disk, or synthesised once."""
        cached = self._audio.get(prompt_id)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(prompt_id, asyncio.Lock())
        async with lock:
            if prompt_id in self._audio:  # another call synthesised it while we waited
                return self._audio[prompt_id]
            pcm = self._from_disk(prompt_id)
            if not pcm:  # missing, or an empty file left by a failed write
                chunks = [chunk async for chunk in tts.stream(self.text(prompt_id), language)]
                pcm = resample(b"".join(chunks), SAMPLE_RATE_TTS, SAMPLE_RATE_TELEPHONY)
                self._to_disk(prompt_id, pcm)
            self._audio[prompt_id] = pcm
            return pcm

    async def prewarm(self, tts: TextToSpeech, language: Language = "uz") -> int:
        """Synthesises every phrase at start-up. Returns how many are ready.

        A failure here is not fatal: the phrase is simply synthesised later, during a call.
        """
        ready = 0
        for prompt_id in self._texts:
            try:
                await self.audio(prompt_id, tts, language)
                ready += 1
            except Exception as exc:
                log.warning("could not prepare prompt %s: %s", prompt_id, exc)
        return ready

    def _cached_path(self, prompt_id: str) -> Path | None:
        """The file name carries a fingerprint of the text and the voice, not just the id.

        Keyed by id alone, an edited phrase kept playing in the caller's ear forever: the
        old file still matched, so nothing was ever re-synthesised, while the CRM showed
        the new wording next to audio of the old one. The voice belongs in the same
        fingerprint for the same reason — changing `VOICELAB_VOICE_UZ` would otherwise
        leave every service phrase in the previous voice, and only the answers would
        change, so the assistant would greet the caller in one voice and answer in
        another. A missed file costs nothing: the synthesis layer beneath keeps its own
        cache, keyed by text, voice and speed together.
        """
        if self._cache_dir is None:
            return None
        key = f"{self.text(prompt_id)}|{self._voice}"
        fingerprint = hashlib.sha256(key.encode()).hexdigest()[:8]
        return self._cache_dir / f"{prompt_id}-{fingerprint}.pcm"

    def _from_disk(self, prompt_id: str) -> bytes | None:
        path = self._cached_path(prompt_id)
        if path is None:
            return None
        try:
            return path.read_bytes() if path.exists() else None
        except OSError as exc:
            log.warning("could not read cached prompt %s: %s", prompt_id, exc)
            return None

    def _to_disk(self, prompt_id: str, pcm: bytes) -> None:
        """Writes through a temporary file: a reader never sees a half-written phrase."""
        path = self._cached_path(prompt_id)
        if path is None:
            return
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(pcm)
            os.replace(temporary, path)
        except OSError as exc:  # read-only deployment, not fatal
            log.warning("could not cache prompt %s: %s", prompt_id, exc)
            temporary.unlink(missing_ok=True)
