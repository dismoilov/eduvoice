"""Failure paths of a call.

Every test here answers the same question: *can the caller end up hearing nothing?*
They exist because an audit found three ways that could happen — an unexpected error in
a turn, a crash before cleanup, and a dead audio socket — and none of them were covered.
"""

import asyncio
import contextlib
import uuid
from dataclasses import replace

import pytest

from eduvoice.audiosocket import KIND_HANGUP, AudioOut, encode
from eduvoice.brain import Brain
from eduvoice.fakes import FakeSpeechToText, FakeTextToSpeech
from eduvoice.interfaces import ChatMessage
from eduvoice.prompts import PromptLibrary
from eduvoice.registry import CallRegistry
from eduvoice.session import CallSession
from eduvoice.vad import BargeInDetector, SpeechDetector
from tests.test_session import CONFIG, PROMPTS, QUIET, SPEECH, StubWriter, is_speech_frame


class BrokenChatModel:
    """Fails the way a real HTTP client does: not with our own error type."""

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        raise ValueError("unexpected provider failure")


class SilentChatModel:
    """Never answers: the model hangs instead of failing."""

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        await asyncio.sleep(3600)
        return {}


class ExplodingWriter(StubWriter):
    """The caller hung up: every write fails, as it does on a closed socket."""

    def __init__(self, fail_after: int = 0) -> None:
        super().__init__()
        self._fail_after = fail_after

    async def drain(self) -> None:
        if len(self.messages) > self._fail_after:
            raise ConnectionResetError("caller hung up")
        await asyncio.sleep(0)


def build(model, writer=None, prompts=None, config=None):
    reader = asyncio.StreamReader()
    writer = writer or StubWriter()
    registry = CallRegistry()
    stt = FakeSpeechToText(["stipendiya qanday olinadi"], latency_ms=10)
    tts = FakeTextToSpeech(first_chunk_ms=10, ms_per_char=2)
    session = CallSession(
        reader,
        writer,  # type: ignore[arg-type]
        stt=stt,
        tts=tts,
        brain=Brain(model, faq={}, timeout_s=0.5),
        prompts=PromptLibrary(prompts or PROMPTS),
        registry=registry,
        config=config or CONFIG,
        speech_detector=SpeechDetector(CONFIG, speech_test=is_speech_frame),
        barge_detector=BargeInDetector(CONFIG, speech_test=is_speech_frame),
    )
    return reader, writer, registry, session


async def wait_for(condition, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def ask(reader, session) -> None:
    await wait_for(lambda: session.state == "listening")
    for frame in [SPEECH] * 10 + [QUIET] * 10:
        reader.feed_data(encode(0x10, frame))


@pytest.mark.parametrize("model", [BrokenChatModel(), SilentChatModel()])
async def test_a_broken_model_transfers_instead_of_leaving_the_caller_in_silence(model):
    """An unexpected error or a hang must not park the call in `thinking` forever."""
    call_id = str(uuid.uuid4())
    reader, writer, registry, session = build(model)
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))

    task = asyncio.create_task(session.run())
    await ask(reader, session)
    await wait_for(lambda: session.state == "closing", timeout=12)
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=5)

    assert registry.next_action(call_id) == "operator"
    assert PROMPTS["tech_problem"] in session._tts.spoken
    assert KIND_HANGUP in writer.kinds(), "the dialplan was never handed the call back"


async def test_the_dialplan_is_told_what_to_do_even_if_the_closing_phrase_is_missing():
    """The decision is written down before anything that can fail is attempted."""
    call_id = str(uuid.uuid4())
    incomplete = {k: v for k, v in PROMPTS.items() if k != "tech_problem"}
    reader, _writer, registry, session = build(BrokenChatModel(), prompts=incomplete)
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))

    task = asyncio.create_task(session.run())
    await ask(reader, session)
    await wait_for(lambda: registry.next_action(call_id) == "operator", timeout=12)
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=5)

    assert registry.next_action(call_id) == "operator"


async def test_cleanup_runs_even_when_the_socket_dies_mid_answer():
    """Sockets, providers and the call record must be released on every path."""
    call_id = str(uuid.uuid4())
    reader, _writer, registry, session = build(BrokenChatModel(), writer=ExplodingWriter())
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))

    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.2)
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=5)

    record = registry.get(call_id)
    assert record is not None and record.ended_at is not None, "the call was never closed"
    assert registry.active == 0, "a finished call is still counted as active"
    assert not session._stt.opened, "the recogniser connection leaked"


async def test_audio_output_survives_a_dead_socket():
    """A write failure must wake up whoever waits for the audio to drain."""
    writer = ExplodingWriter()
    out = AudioOut(writer)  # type: ignore[arg-type]
    out.start()
    out.write(b"\x01" * 3200)

    drained = await asyncio.wait_for(out.wait_drained(timeout=2), timeout=3)
    await out.aclose()

    assert drained, "wait_drained() hung after the socket died"
    assert out.failed


async def test_wait_drained_times_out_instead_of_hanging():
    writer = StubWriter()
    out = AudioOut(writer)  # type: ignore[arg-type]
    out.write(b"\x01" * 320)  # never started: nothing will ever send it

    assert await out.wait_drained(timeout=0.05) is False
    await out.aclose()


class DeafSpeechToText:
    """Recognition that always hears nothing — a noisy line, or a caller speaking Russian.

    This is not an error: the real provider answers `no_speech_detected` and the bridge is
    meant to ask the caller to repeat. The danger is asking forever.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def open(self, language: str) -> None:
        return None

    async def transcribe(self, pcm16k: bytes):
        from eduvoice.interfaces import Transcript

        self.calls += 1
        return Transcript(text="", language="uz", duration_ms=0, latency_ms=1)

    async def close(self) -> None:
        return None


async def test_speech_that_is_never_recognised_ends_with_a_person_not_an_endless_loop():
    """Each lap costs a paid recognition, so the loop must be bounded and end with a human.

    Before this was fixed the caller could circle for the whole six-minute call limit: the
    turn counter only advanced on a *recognised* utterance, so nothing ever escalated.
    """
    call_id = str(uuid.uuid4())
    reader, _writer, registry, session = build(BrokenChatModel())
    session._stt = DeafSpeechToText()
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))

    task = asyncio.create_task(session.run())
    for _ in range(6):  # keep talking into a line that recognises nothing
        if session.state == "closing":
            break
        await ask(reader, session)
        await asyncio.sleep(0.15)
    await wait_for(lambda: session.state == "closing", timeout=12)
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=5)

    assert registry.next_action(call_id) == "operator", "the caller must reach a person"
    assert session._stt.calls <= 4, f"paid recognition ran {session._stt.calls} times"


async def test_a_long_answer_is_given_time_to_be_synthesised():
    """The provider renders the whole phrase before sending any of it.

    So the wait for the first sound grows with the length of the answer. A flat deadline
    fits a greeting and cuts off a full regulation — and a synthesis cut off that way is
    paid for, never cached, and ends with the caller handed to an operator.
    """
    _reader, _writer, _registry, session = build(BrokenChatModel())

    short = session._first_chunk_budget("Salom.")
    regulation = session._first_chunk_budget("A" * 300)

    assert short == pytest.approx(CONFIG.tts_first_chunk_timeout_s, abs=0.1)
    assert regulation > short + 2, "a 300-character answer got no more time than a greeting"
    # Measured on the live service: 214 characters took 1.40 s end to end.
    assert session._first_chunk_budget("A" * 214) > 1.4


class SlowChatModel:
    """Takes long enough that the filler is played while the caller waits."""

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        await asyncio.sleep(0.6)
        return {"intent": "faq", "faq_id": "stipend"}


async def test_the_caller_can_interrupt_the_filler_they_are_listening_to():
    """ "One moment, I am checking" is the assistant speaking, so it can be spoken over.

    Frames arriving while the answer was being prepared were dropped, and the guard was
    disarmed at the same moment — so a caller saying "never mind, put me through to a
    person" over the filler was ignored twice: once while they spoke, and again when
    their words were thrown away before the answer began.
    """
    call_id = str(uuid.uuid4())
    quick_filler = replace(CONFIG, filler_after_s=0.05)
    reader, _writer, _registry, session = build(SlowChatModel(), config=quick_filler)
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))

    task = asyncio.create_task(session.run())
    await ask(reader, session)
    await wait_for(lambda: session.state == "thinking")
    await wait_for(lambda: session._out.speaking, timeout=4)  # the filler is being played

    for frame in [SPEECH] * 30:  # the caller cuts in over it
        reader.feed_data(encode(0x10, frame))
    await wait_for(lambda: session.state == "listening", timeout=4)

    assert session._think_task is None, "the abandoned turn was left running"
    reader.feed_eof()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=5)
