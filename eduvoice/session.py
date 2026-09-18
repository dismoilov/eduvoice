"""One call: greeting, listening, thinking, speaking, transfer or goodbye.

The frame loop in `run()` is the clock of the call. Everything slow (recognition, the
model, synthesis) runs in tasks so that incoming audio is never blocked — otherwise
barge-in would arrive too late and the caller would talk over a bot that cannot hear.

States and what moves the call between them::

    greeting  -- the greeting is playing; only barge-in is watched
       |  playback finished / caller interrupted
       v
    listening -- frames go to the voice detector; silence triggers a reprompt
       |  a finished phrase
       v
    thinking  -- recognition + decision run in a task; a filler is played after 1.2 s
       |  decision
       v
    speaking  -- the answer is playing; barge-in is watched again -> listening
       |  transfer / goodbye / any failure
       v
    closing   -- closing phrase, the next action is written down, socket closed

The one invariant that matters more than anything else: **the caller is never left in
silence**. Every failure path ends in `_finish("operator", ...)`, and the next action is
recorded in the registry *before* anything that can fail, so even a crash at the very
last moment still sends the caller to a human (see eduvoice/control_api.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Literal

from eduvoice.audio import FrameSplitter, StreamResampler, resample
from eduvoice.audiosocket import KIND_HANGUP, KIND_UUID, AudioOut, encode, read_frames
from eduvoice.brain import Brain, Decision
from eduvoice.calllog import CallLog, call_as_dict
from eduvoice.config import Settings
from eduvoice.interfaces import (
    SAMPLE_RATE_STT,
    SAMPLE_RATE_TELEPHONY,
    SAMPLE_RATE_TTS,
    ProviderError,
    SpeechToText,
    TextToSpeech,
)
from eduvoice.prompts import PromptLibrary
from eduvoice.registry import CallRegistry, Turn
from eduvoice.vad import BargeInDetector, SpeechDetector, Utterance
from store.write import CallStore

log = logging.getLogger("eduvoice.session")

State = Literal["greeting", "listening", "thinking", "speaking", "closing"]

# How long the closing phrase may take to reach the caller before we give up and let
# the dialplan take over. Generous, because this is the last thing the caller hears.
CLOSING_DRAIN_TIMEOUT_S = 15.0
# How many utterances in a row may come back unrecognised before a person takes over.
# Without a limit the call loops: the detector fires on noise, recognition hears nothing,
# the assistant asks again — six minutes of that, and every lap is a paid recognition.
MAX_UNHEARD = 3
# Characters of text the synthesiser is assumed to render per second, on top of the flat
# first-chunk allowance. Measured at ~150/s on the live service; 100 leaves a margin.
CHARS_PER_SECOND_OF_BUDGET = 100.0


class CallSession:
    """Runs one call from the first AudioSocket frame to the hangup."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        stt: SpeechToText,
        tts: TextToSpeech,
        brain: Brain,
        prompts: PromptLibrary,
        registry: CallRegistry,
        config: Settings,
        speech_detector: SpeechDetector | None = None,
        barge_detector: BargeInDetector | None = None,
        store: CallStore | None = None,
    ) -> None:
        self.call_id = ""
        self._reader = reader
        self._writer = writer
        self._stt = stt
        self._tts = tts
        self._brain = brain
        self._prompts = prompts
        self._registry = registry
        self._cfg = config

        self._out = AudioOut(writer)
        # Asterisk does not guarantee 20 ms chunks (chan_audiosocket splits differently),
        # and webrtcvad only accepts 10/20/30 ms frames — so normalise the stream here.
        self._incoming = FrameSplitter(SAMPLE_RATE_TELEPHONY)
        self._logged_chunk_size = False
        self._speech = speech_detector or SpeechDetector(config)
        self._barge = barge_detector or BargeInDetector(config)
        # Frames heard while the bot is talking. On barge-in they are replayed into the
        # voice detector, otherwise the first word of the interruption would be lost.
        self._recent = deque[bytes](maxlen=max(1, config.barge_in_window))
        self._state: State = "greeting"
        self._speak_task: asyncio.Task[None] | None = None
        self._think_task: asyncio.Task[None] | None = None
        self._filler_task: asyncio.Task[None] | None = None
        self._last_answer = ""
        self._turn_index = 0
        self._reprompts = 0
        self._unheard = 0
        self._finishing = False
        self._cleaned_up = False
        self._started = time.monotonic()
        self._ended_reason = "caller_hangup"
        self._call_log = CallLog(config.call_log_dir)
        # The CRM reads calls from the database; the JSON log stays as the backup.
        self._store = store
        # Hard budget for one turn (recognition + decision). Both stages have their own
        # timeout; this one also covers anything unexpected in between.
        self._turn_budget_s = config.stt_timeout_s + config.llm_timeout_s + 3.0

    @property
    def state(self) -> State:
        """Current phase of the call; used by the tests."""
        return self._state

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        """Drives one call. Asterisk always sends the UUID frame first.

        `_cleanup` is in a `finally`: whatever happens, the sockets, tasks and the call
        log are closed properly, and the registry stops counting the call as active.
        """
        self._out.start()
        try:
            async for frame in read_frames(self._reader):
                if frame.is_hangup:
                    log.info("call %s: caller hung up", self.call_id or "unknown")
                    self._ended_reason = "caller_hangup"
                    break
                if frame.kind == KIND_UUID and not self.call_id:
                    await self._begin(frame.call_id())
                    continue
                if not frame.is_audio or not self.call_id:
                    continue
                if self._state == "closing":
                    continue
                await self._on_incoming_audio(frame.payload)
                if time.monotonic() - self._started > self._cfg.max_call_s:
                    log.info("call %s: time limit reached -> operator", self.call_id)
                    await self._finish("operator", "transfer")
                    break
        finally:
            await self._cleanup()

    async def _begin(self, call_id: str) -> None:
        """First frame of the call: register it, open the providers, greet the caller."""
        self.call_id = call_id
        record = self._registry.ensure(call_id)
        record.connected = True
        # The dialplan records every call as eduvoice/<date>/<uuid>.wav (MixMonitor); writing
        # the path down here is what lets the CRM find the audio later.
        day = time.strftime("%Y%m%d", time.localtime(record.started_at))
        record.recording = f"eduvoice/{day}/{call_id}.wav"
        log.info("call %s started (caller=%s)", call_id, record.caller or "unknown")
        try:
            await self._stt.open("uz")
            await self._tts.warm_up("uz")
        except Exception as exc:  # no providers, no assistant: hand over straight away
            log.error("call %s: providers unavailable (%s) -> operator", call_id, exc)
            await self._finish("operator", "tech_problem")
            return
        self._speak_prompt("greeting")

    # -------------------------------------------------------------- frames

    async def _on_incoming_audio(self, chunk: bytes) -> None:
        """Splits whatever Asterisk sent into exact 20 ms frames and feeds the detectors."""
        if not self._logged_chunk_size:
            log.info("call %s: Asterisk sends %d-byte chunks", self.call_id, len(chunk))
            self._logged_chunk_size = True
        for frame in self._incoming.push(chunk):
            await self._on_audio(frame)

    async def _on_audio(self, frame: bytes) -> None:
        if self._state in ("greeting", "speaking"):
            self._recent.append(frame)
            if self._barge.push(frame):
                log.info("call %s: barge-in", self.call_id)
                await self._stop_speaking()
                self._listen()
                # Replay what the caller already said over us, so recognition gets the
                # whole phrase and not just its tail.
                self._speech.seed(list(self._recent))
                self._recent.clear()
            return

        if self._state == "thinking":
            return  # audio during thinking is dropped on purpose: the answer is coming

        utterance = self._speech.push(frame)
        if utterance is not None:
            self._think(utterance)
            return

        await self._check_silence()

    async def _check_silence(self) -> None:
        """Nobody is speaking: ask once more, then say goodbye rather than hold the line."""
        if self._speech.silence_ms < self._cfg.silence_reprompt_s * 1000:
            return
        self._speech.reset()
        self._reprompts += 1
        if self._reprompts == 1:
            self._speak_prompt("reprompt")
        else:
            log.info("call %s: no answer twice -> goodbye", self.call_id)
            await self._finish("hangup", "goodbye")

    # ------------------------------------------------------------- talking

    def _listen(self) -> None:
        self._state = "listening"
        self._speech.reset()

    def _speak_prompt(self, prompt_id: str) -> None:
        """Play a service phrase (cached audio, no synthesis delay)."""
        self._speak("", prompt_id=prompt_id)

    def _speak(self, text: str, prompt_id: str | None = None) -> None:
        self._state = "greeting" if prompt_id == "greeting" else "speaking"
        self._barge.playback_started()
        self._recent.clear()
        self._speak_task = asyncio.create_task(self._run_speech(text, prompt_id))

    async def _run_speech(self, text: str, prompt_id: str | None) -> None:
        """Sends one phrase to the caller, then goes back to listening.

        A service phrase comes from the prompt cache as one buffer; a generated answer is
        streamed chunk by chunk through a single resampler, so playback starts as soon as
        the first chunk arrives instead of after the whole sentence.
        """
        try:
            if prompt_id is not None:
                self._out.write(
                    await asyncio.wait_for(
                        self._prompts.audio(prompt_id, self._tts),
                        timeout=self._cfg.tts_first_chunk_timeout_s + 5.0,
                    )
                )
            else:
                await self._stream_answer(text)
            await self._out.wait_drained(timeout=self._cfg.max_call_s)
        except asyncio.CancelledError:
            raise
        except (ProviderError, TimeoutError) as exc:
            log.warning("call %s: speech failed (%s) -> operator", self.call_id, exc)
            await self._finish("operator", "tech_problem")
            return
        except Exception:
            log.exception("call %s: unexpected error while speaking -> operator", self.call_id)
            await self._finish("operator", "tech_problem")
            return
        finally:
            if self._state in ("greeting", "speaking"):
                self._listen()

    def _first_chunk_budget(self, text: str) -> float:
        """How long to wait for the first sound, allowing for how much there is to say.

        The provider does not stream: it renders the whole phrase and sends it at once,
        so the first chunk cannot arrive sooner than the last. Measured against the live
        service on 18.09: 23 characters in 0.73 s, 75 in 0.86 s, 214 in 1.40 s. A flat
        three-second deadline therefore fits a short reply comfortably and cuts off a long
        one — and a synthesis cut off this way is billed, uncached, and ends with the
        caller handed to an operator.
        """
        return self._cfg.tts_first_chunk_timeout_s + len(text) / CHARS_PER_SECOND_OF_BUDGET

    async def _stream_answer(self, text: str) -> None:
        """Streams synthesised speech, 24 kHz -> 8 kHz, with a deadline on the first chunk."""
        converter = StreamResampler(SAMPLE_RATE_TTS, SAMPLE_RATE_TELEPHONY)
        chunks = self._tts.stream(text, "uz").__aiter__()
        first_budget = self._first_chunk_budget(text)
        first = True
        try:
            while True:
                try:
                    timeout = first_budget if first else self._cfg.max_call_s
                    chunk = await asyncio.wait_for(chunks.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    break
                first = False
                self._out.write(converter.push(chunk))
            self._out.write(converter.flush())
        finally:
            # Barge-in cancels this task: closing the iterator stops the synthesis too,
            # so we are not billed for audio nobody will hear.
            aclose = getattr(chunks, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()

    async def _stop_speaking(self) -> None:
        """Barge-in / transfer: drop queued audio and cancel the speaking task."""
        self._out.clear()
        task, self._speak_task = self._speak_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------ thinking

    def _think(self, utterance: Utterance) -> None:
        """Starts the turn: recognition and decision in one task, a filler in another."""
        self._state = "thinking"
        self._reprompts = 0
        self._think_task = asyncio.create_task(self._run_turn(utterance))
        self._filler_task = asyncio.create_task(self._filler_after_delay())

    async def _filler_after_delay(self) -> None:
        """ "One moment, I am checking" — played only if the answer is not ready in time."""
        await asyncio.sleep(self._cfg.filler_after_s)
        if self._state != "thinking":
            return
        with contextlib.suppress(Exception):
            pcm = await self._prompts.audio("filler", self._tts)
            if self._state == "thinking":  # the answer may have arrived while synthesising
                self._out.write(pcm)

    async def _run_turn(self, utterance: Utterance) -> None:
        """One turn: recognise, decide, act. Any failure hands the caller to a human.

        The whole decision part runs under a single budget, so no provider can leave the
        call parked in `thinking` with nothing but silence on the line.
        """
        try:
            decision = await asyncio.wait_for(self._decide(utterance), self._turn_budget_s)
        except asyncio.CancelledError:
            raise
        except (ProviderError, TimeoutError) as exc:
            log.warning("call %s: turn failed (%s) -> operator", self.call_id, exc)
            await self._finish("operator", "tech_problem")
            return
        except Exception:
            log.exception("call %s: unexpected error in a turn -> operator", self.call_id)
            await self._finish("operator", "tech_problem")
            return
        finally:
            self._cancel_filler()

        if decision is None:  # nothing was recognised
            self._unheard += 1
            if self._unheard >= MAX_UNHEARD:
                log.info(
                    "call %s: %d utterances in a row could not be recognised -> operator",
                    self.call_id,
                    self._unheard,
                )
                await self._finish("operator", "transfer")
                return
            self._speak_prompt("repeat_please")
            return
        self._unheard = 0

        try:
            await self._act(decision)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("call %s: could not act on the decision -> operator", self.call_id)
            await self._finish("operator", "tech_problem")

    async def _decide(self, utterance: Utterance) -> Decision | None:
        """Recognition + decision. Returns None when the caller said nothing recognisable."""
        started = time.monotonic()
        latency: dict[str, float] = {"utterance_ms": utterance.duration_ms}

        pcm16k = resample(utterance.pcm8k, SAMPLE_RATE_TELEPHONY, SAMPLE_RATE_STT)
        transcript = await asyncio.wait_for(
            self._stt.transcribe(pcm16k), timeout=self._cfg.stt_timeout_s
        )
        latency["stt_ms"] = (time.monotonic() - started) * 1000

        log.info("call %s: heard %r", self.call_id, transcript.text)
        if not transcript.text.strip():
            return None

        decision = await self._brain.decide(transcript.text, turn_index=self._turn_index)
        latency["decision_ms"] = decision.latency_ms
        latency["total_ms"] = (time.monotonic() - started) * 1000
        self._turn_index += 1
        log.info(
            "call %s: decision=%s intent=%s (%.0f ms total)",
            self.call_id,
            decision.action,
            decision.intent,
            latency["total_ms"],
        )
        self._registry.ensure(self.call_id).turns.append(
            Turn(
                question=transcript.text,
                answer=decision.text,
                intent=decision.intent,
                latency_ms=latency,
                action=decision.action,
                faq_id=decision.faq_id or "",
                source=decision.source,
                # Where the question starts in the recording: now, minus how long the
                # caller was speaking, minus how long recognition and the model took.
                at_ms=max(0.0, (started - self._started) * 1000 - utterance.duration_ms),
            )
        )
        return decision

    def _cancel_filler(self) -> None:
        task, self._filler_task = self._filler_task, None
        if task is not None:
            task.cancel()

    async def _act(self, decision: Decision) -> None:
        """Turns a decision into what the caller actually hears."""
        match decision.action:
            case "transfer":
                prompt = "tech_problem" if decision.intent == "tech_problem" else "transfer"
                self._note_spoken(prompt)
                await self._finish("operator", prompt)
            case "goodbye":
                self._note_spoken("goodbye")
                await self._finish("hangup", "goodbye")
            case "repeat":
                if self._last_answer:
                    self._speak(self._last_answer)
                else:
                    self._note_spoken("repeat_please")
                    self._speak_prompt("repeat_please")
            case "clarify":
                prompt = "out_of_scope" if decision.intent == "out_of_scope" else "not_understood"
                self._note_spoken(prompt)
                self._speak_prompt(prompt)
            case _:
                self._last_answer = decision.text
                self._speak(decision.text)

    def _note_spoken(self, prompt_id: str) -> None:
        """Writes the service phrase into the turn, so the CRM shows what was actually said.

        A transfer or a goodbye carries no text of its own — without this the supervisor
        would see an empty answer next to the question.
        """
        record = self._registry.get(self.call_id)
        if record is None or not record.turns or record.turns[-1].answer:
            return
        with contextlib.suppress(KeyError):
            record.turns[-1].answer = self._prompts.text(prompt_id)

    # -------------------------------------------------------------- ending

    async def _finish(self, next_action: str, prompt_id: str) -> None:
        """Say the closing phrase, tell the dialplan what to do, then close the socket.

        The decision for the dialplan is written down *first*. Everything after it may
        fail or be cancelled (the caller can hang up at any moment) without the caller
        ever being dropped: the dialplan reads `operator` unless we explicitly said
        `hangup`. Reentrant calls are ignored, so two failure paths cannot both close
        the same call.
        """
        if self._finishing:
            return
        self._finishing = True
        self._state = "closing"
        self._ended_reason = prompt_id
        self._registry.set_next(self.call_id, "hangup" if next_action == "hangup" else "operator")
        log.info("call %s: handing back to dialplan as %s", self.call_id, next_action)

        self._cancel_filler()
        await self._stop_speaking()
        try:
            pcm = await asyncio.wait_for(
                self._prompts.audio(prompt_id, self._tts),
                timeout=self._cfg.tts_first_chunk_timeout_s + 5.0,
            )
            self._out.write(pcm)
            await self._out.wait_drained(timeout=CLOSING_DRAIN_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("call %s: closing phrase failed (%s)", self.call_id, exc)
        finally:
            with contextlib.suppress(OSError, RuntimeError):
                self._writer.write(encode(KIND_HANGUP))

    async def aclose(self) -> None:
        """Releases the call's resources. Safe to call twice; `run()` does it itself."""
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Releases everything this call owns. Runs exactly once, even after a crash."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._cancel_filler()
        await self._stop_speaking()
        if self._think_task is not None and self._think_task is not asyncio.current_task():
            self._think_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._think_task
        self._think_task = None
        await self._out.aclose()
        for provider in (self._stt, self._tts):
            with contextlib.suppress(Exception):
                await provider.close()
        self._registry.finish(self.call_id)
        with contextlib.suppress(OSError):
            self._writer.close()
        record = self._registry.get(self.call_id)
        if record is not None:
            entry = call_as_dict(record, self._ended_reason)
            self._call_log.write_entry(entry)
            if self._store is not None:
                self._store.save(entry)
        log.info(
            "call %s ended after %.1f s, %d turns (%s)",
            self.call_id or "unknown",
            record.duration_s if record else 0.0,
            len(record.turns) if record else 0,
            self._ended_reason,
        )
