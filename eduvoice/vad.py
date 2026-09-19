"""Turn taking: when the caller starts speaking, when the phrase ends, and barge-in.

VoiceLab returns no interim transcripts, so the bridge itself has to decide where an
utterance ends. Decisions are made on 20 ms frames of 8 kHz telephone audio with
hysteresis, so one noisy frame never starts or ends a phrase.

The speech test is injectable: production uses webrtcvad, tests use a deterministic stub.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from eduvoice.audio import FRAME_MS, duration_ms, frame_bytes
from eduvoice.config import Settings
from eduvoice.config import settings as default_settings
from eduvoice.interfaces import SAMPLE_RATE_TELEPHONY

log = logging.getLogger("eduvoice.vad")

KEEP_TAIL_FRAMES = 5  # 100 ms of silence kept at the end of an utterance


class SpeechTest(Protocol):
    def __call__(self, frame: bytes) -> bool: ...


class WebrtcSpeechTest:
    """webrtcvad: the only VAD that installs on the server (glibc 2.17)."""

    def __init__(self, aggressiveness: int = 2, sample_rate: int = SAMPLE_RATE_TELEPHONY) -> None:
        import webrtcvad  # imported here so tests can run without the extension

        self._vad = webrtcvad.Vad(aggressiveness)
        self._sample_rate = sample_rate

    def __call__(self, frame: bytes) -> bool:
        try:
            return self._vad.is_speech(frame, self._sample_rate)
        except Exception as exc:  # wrong frame size: treat as silence, never kill the call
            log.warning("VAD rejected a %d-byte frame: %s", len(frame), exc)
            return False


@dataclass(frozen=True, slots=True)
class Utterance:
    """One finished phrase, ready to be sent for recognition."""

    pcm8k: bytes
    ended_by: str  # "silence" | "max_length"

    @property
    def duration_ms(self) -> float:
        return duration_ms(self.pcm8k, SAMPLE_RATE_TELEPHONY)


class LoudnessGate:
    """Is this frame louder than the room the caller is standing in?

    `webrtcvad` answers "is this speech-shaped", and in a hall with music and a crowd it
    answers yes to every single frame — measured on a real call from the venue: 100 % of
    frames called speech at the strictest setting, background steady at RMS 3400 with no
    quiet moment at all. A phrase can then never end, because ending one needs silence.

    A handset is centimetres from the mouth, so the caller's own voice arrives far louder
    than the room. That difference is what this measures. The floor follows the room down
    at once and climbs back slowly, so a lull does not raise the bar.
    """

    def __init__(self, margin: float, window_frames: int = 100, quietest: float = 30.0) -> None:
        self._margin = margin
        self._recent = deque[float](maxlen=window_frames)
        self._quietest = quietest

    @staticmethod
    def level(frame: bytes) -> float:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0

    def __call__(self, frame: bytes) -> bool:
        """The room is the quietest thing heard in the last couple of seconds.

        Tracked over a short window rather than as a slowly drifting average: walking into
        a hall changes the background in one step, and an average takes a quarter of a
        minute to catch up — the whole call, in other words. Speech has dips between
        syllables, so even continuous talking leaves the window near the room level.
        """
        rms = self.level(frame)
        self._recent.append(rms)
        floor = max(self._floor_of(self._recent), self._quietest)
        return rms >= floor * self._margin

    @staticmethod
    def _floor_of(levels: deque[float]) -> float:
        """The tenth percentile, not the minimum.

        The first frame of a call arrives before any room audio does — measured at 149
        against a hall sitting at 4000 — and a plain minimum let that one frame set the
        bar for the next two seconds, so the room cleared it easily and cut the answer
        off half a second in.
        """
        ordered = sorted(levels)
        return ordered[len(ordered) // 10]


class SpeechDetector:
    """Collects one utterance: pre-roll + speech, ends after a fixed silence."""

    def __init__(
        self, config: Settings | None = None, speech_test: SpeechTest | None = None
    ) -> None:
        cfg = config or default_settings
        vad = speech_test or WebrtcSpeechTest(cfg.vad_aggressiveness)
        if speech_test is None and cfg.loudness_margin > 1.0:
            gate = LoudnessGate(cfg.loudness_margin, quietest=cfg.loudness_floor_min)

            def heard(frame: bytes) -> bool:
                # The gate runs on every frame, whatever the detector thinks, or the room
                # would only be measured while somebody was already talking.
                loud_enough = gate(frame)
                return loud_enough and vad(frame)

            self._is_speech: SpeechTest = heard
        else:
            self._is_speech = vad
        self._start_needed = cfg.speech_start_frames
        self._start_window = deque[bool](maxlen=cfg.speech_start_window)
        self._silence_to_end = cfg.silence_end_frames
        self._max_frames = int(cfg.max_utterance_s * 1000 / FRAME_MS)
        self._preroll = deque[bytes](maxlen=max(1, int(cfg.preroll_ms / FRAME_MS)))
        self._utterance = bytearray()
        self._frame_bytes = frame_bytes(SAMPLE_RATE_TELEPHONY)
        self._silence_frames = 0
        self._idle_frames = 0
        self.in_speech = False

    def reset(self) -> None:
        self._start_window.clear()
        self._preroll.clear()
        self._utterance.clear()
        self._silence_frames = 0
        self._idle_frames = 0
        self.in_speech = False

    def seed(self, frames: list[bytes]) -> list[Utterance]:
        """Replay what the caller said while we were talking, as if it had just arrived.

        Returns any phrase that *finished* during the replay — someone who asked their
        whole question over the greeting and then waited. Those used to be dropped on the
        floor: `push` returned them and nobody looked, so the caller was answered with
        "say your question" over a question they had just asked in full.

        Silence inside the replay is not the caller's silence either. They were waiting
        for us to stop, and counting it made the reprompt fire the instant we did — a
        six-second greeting was followed immediately by "say your question".
        """
        found: list[Utterance] = []
        for frame in frames:
            if len(frame) == self._frame_bytes:
                utterance = self.push(frame)
                if utterance is not None:
                    found.append(utterance)
        self._idle_frames = 0
        return found

    @property
    def silence_ms(self) -> float:
        """How long the caller has been quiet while we are waiting for a phrase."""
        return self._idle_frames * FRAME_MS

    def push(self, frame: bytes) -> Utterance | None:
        speech = self._is_speech(frame)

        if not self.in_speech:
            self._preroll.append(frame)
            self._start_window.append(speech)
            self._idle_frames = 0 if speech else self._idle_frames + 1
            if sum(self._start_window) >= self._start_needed:
                self.in_speech = True
                self._utterance.extend(b"".join(self._preroll))
                self._preroll.clear()
                self._start_window.clear()
                self._silence_frames = 0
            return None

        self._utterance.extend(frame)
        self._silence_frames = 0 if speech else self._silence_frames + 1

        if self._silence_frames >= self._silence_to_end:
            return self._finish("silence")
        if len(self._utterance) >= self._max_frames * self._frame_bytes:
            return self._finish("max_length")
        return None

    def _finish(self, reason: str) -> Utterance:
        pcm = bytes(self._utterance)
        if reason == "silence":
            # Drop the trailing silence but keep KEEP_TAIL_FRAMES (100 ms) so that a quiet
            # last syllable is not clipped off before recognition.
            trim_frames = max(0, self._silence_frames - KEEP_TAIL_FRAMES)
            trim = min(len(pcm), trim_frames * self._frame_bytes)
            pcm = pcm[: len(pcm) - trim]
        self.reset()
        return Utterance(pcm8k=pcm, ended_by=reason)


class BargeInDetector:
    """Says whether the caller is talking over the bot.

    A short guard after playback starts ignores line echo of our own voice, and the same
    loudness test the phrase detector uses keeps a room from interrupting us: without it
    the assistant heard the question, began to answer, and was cut off by the hall within
    a second — every single time, so the caller never heard a word of the answer.
    """

    def __init__(
        self, config: Settings | None = None, speech_test: SpeechTest | None = None
    ) -> None:
        cfg = config or default_settings
        vad = speech_test or WebrtcSpeechTest(cfg.vad_aggressiveness)
        if speech_test is None and cfg.barge_in_margin > 1.0:
            gate = LoudnessGate(cfg.barge_in_margin, quietest=cfg.loudness_floor_min)

            def heard(frame: bytes) -> bool:
                loud_enough = gate(frame)
                return loud_enough and vad(frame)

            self._is_speech: SpeechTest = heard
        else:
            self._is_speech = vad
        self._needed = cfg.barge_in_frames
        self._window = deque[bool](maxlen=cfg.barge_in_window)
        self._guard_frames = int(cfg.barge_in_guard_ms / FRAME_MS)
        self._frames_since_start = 0
        self._audible = False

    def playback_started(self) -> None:
        """A phrase has been asked for — nothing is in the caller's ear yet."""
        self._window.clear()
        self._frames_since_start = 0
        self._audible = False

    def playback_audible(self) -> None:
        """The first sound of it has actually gone down the line; the guard starts here.

        Starting the guard when the phrase was *requested* spent it on the silence while
        the answer was being synthesised. The caller saying "hello?" into that gap then
        cancelled an answer they had not heard one sample of — paid for, uncached, and the
        question asked again. Nobody can interrupt what has not been played.
        """
        self._frames_since_start = 0
        self._audible = True

    def push(self, frame: bytes) -> bool:
        """True when the caller has been speaking long enough to interrupt."""
        if not self._audible:
            return False
        self._frames_since_start += 1
        speech = self._is_speech(frame)
        self._window.append(speech)
        if self._frames_since_start <= self._guard_frames:
            return False
        return sum(self._window) >= self._needed
