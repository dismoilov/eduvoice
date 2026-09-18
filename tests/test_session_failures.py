"""Failure paths of a call.

Every test here answers the same question: *can the caller end up hearing nothing?*
They exist because an audit found three ways that could happen — an unexpected error in
a turn, a crash before cleanup, and a dead audio socket — and none of them were covered.
"""

import asyncio
import uuid

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


def build(model, writer=None, prompts=None):
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
        config=CONFIG,
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
