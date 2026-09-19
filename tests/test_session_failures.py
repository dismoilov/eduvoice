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
from eduvoice.registry import CallRegistry, Turn
from eduvoice.session import CallSession
from eduvoice.vad import BargeInDetector, SpeechDetector, Utterance
from tests.test_session import CONFIG, PROMPTS, QUIET, SPEECH, StubWriter, is_speech_frame


class BrokenChatModel:
    """Fails the way a real HTTP client does: not with our own error type."""

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        raise ValueError("unexpected provider failure")


class FarewellModel:
    """Answers every utterance with a goodbye, so `_finish("hangup", …)` is exercised."""

    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        return {"intent": "goodbye"}


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
    """Long enough to be a question: anything under 600 ms is dropped as a fragment,
    because the recogniser refuses audio under half a second."""
    await wait_for(lambda: session.state == "listening")
    for frame in [SPEECH] * 40 + [QUIET] * 10:
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
    """The decision is written down before anything that can fail is attempted.

    Checked on the goodbye, not the transfer: "operator" is what the registry answers for
    a call it has never heard of, so a transfer test passes just as well when the
    decision is never written at all. "hangup" can only come from `_finish`.
    """
    call_id = str(uuid.uuid4())
    incomplete = {k: v for k, v in PROMPTS.items() if k != "goodbye"}
    reader, _writer, registry, session = build(FarewellModel(), prompts=incomplete)
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))

    task = asyncio.create_task(session.run())
    await ask(reader, session)
    await wait_for(lambda: registry.next_action(call_id) == "hangup", timeout=12)
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=5)

    assert registry.next_action(call_id) == "hangup", (
        "the caller said goodbye and the dialplan was told to fetch a human"
    )


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


async def test_two_callers_ending_the_same_call_write_it_down_once():
    """`run()` finishing and a stop signal can both reach cleanup at the same moment.

    Cleanup is one task, shielded from whoever awaits it, so the conversation is written
    exactly once and the writing finishes even if every waiter goes away. The interleaving
    that loses a call is hard to force in a test — the version of this that mattered was
    observed on the server, where restarting the bridge during a live call left no
    database row, no line in the journal that is meant to be the backup, and a recording
    with nothing pointing at it.
    """
    call_id = str(uuid.uuid4())
    reader, _writer, registry, session = build(BrokenChatModel())
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))
    task = asyncio.create_task(session.run())
    await wait_for(lambda: session.state == "listening")
    registry.ensure(call_id).turns.append(
        Turn(question="Stipendiya?", answer="Javob.", intent="faq_keyword", action="faq")
    )
    session._speak("Uzun javob matni, hali aytilmagan qismi bor.")
    await wait_for(lambda: session.state == "speaking")

    first = asyncio.create_task(session.aclose())
    second = asyncio.create_task(session.aclose())
    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first
    await second

    import time as _time

    journal = CONFIG.call_log_dir / f"{_time.strftime('%Y%m%d')}.jsonl"
    written = [line for line in journal.read_text().splitlines() if call_id in line]
    assert len(written) == 1, f"the conversation was written {len(written)} times"
    assert "Stipendiya?" in written[0]
    assert session._cleaned_up, "the cleanup did not finish"
    reader.feed_eof()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=5)


async def test_audio_in_a_format_the_bridge_cannot_decode_goes_to_a_person_at_once():
    """G.722 has exactly the byte rate of G.711, so decoded as ulaw it is a constant roar
    with the caller's voice buried inside. That is what a softphone offered wideband
    produced on the server: the greeting cut off by the roar, twenty seconds of a clearly
    spoken question, and "I did not understand you" in reply. The endpoint is now limited
    to G.711, and a format the bridge cannot decode is refused out loud rather than
    listened to.
    """
    call_id = str(uuid.uuid4())
    reader, writer, registry, session = build(BrokenChatModel())
    registry.start(call_id).audio_format = "g722"
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))

    task = asyncio.create_task(session.run())
    for _ in range(5):
        reader.feed_data(encode(0x10, SPEECH))
    await wait_for(lambda: session.state == "closing", timeout=12)
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=5)

    assert registry.next_action(call_id) == "operator"
    assert KIND_HANGUP in writer.kinds(), "the dialplan was never handed the call back"


async def test_interrupting_the_answer_keeps_the_whole_interrupting_phrase():
    """The one case the suite never covered at session level, which is how two
    regressions reached the server today.

    `_listen` is reached twice on an interruption — from the frame loop and from
    `_run_speech`'s `finally`. The second call used to `reset()` the detector, throwing
    away the frames the first had just replayed, so the start of every interruption was
    clipped: "operator bilan gaplashmoqchiman" arrived as "...bilan gaplashmoqchiman".
    """
    call_id = str(uuid.uuid4())
    reader, _writer, _registry, session = build(BrokenChatModel())
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))
    task = asyncio.create_task(session.run())
    await wait_for(lambda: session.state == "listening")

    session._speak("Uzun javob matni, hali aytilmagan qismi bor.")
    await wait_for(lambda: session.state == "speaking")
    for _ in range(40):  # the caller talks over the answer
        reader.feed_data(encode(0x10, SPEECH))
    await wait_for(lambda: session.state != "speaking", timeout=5)

    kept = len(session._speech._utterance) // len(SPEECH)
    assert kept >= 30, f"only {kept} of 40 frames of the interruption survived"
    reader.feed_eof()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=5)


async def test_a_question_finished_over_the_answer_is_not_undone_by_the_second_listen():
    """If the replay completes a phrase, `_listen` starts the turn. The second call must
    not knock the session back to `listening`: that killed the filler and let a reprompt
    fire over the answer being prepared for the very question just asked."""
    _reader, _writer, _registry, session = build(SlowChatModel())
    session._state = "speaking"
    for _ in range(40):
        session._recent.append(SPEECH)
    for _ in range(50):
        session._recent.append(QUIET)

    session._listen()
    started = session.state
    session._listen()

    assert started == "thinking", "a finished question did not start a turn"
    assert session.state == "thinking", "the second call undid the turn"
    await session.aclose()


async def test_a_fragment_of_room_noise_is_not_sent_for_recognition():
    """The service refuses audio under half a second, and the bridge read that refusal as
    the provider failing — so a cough during the greeting ended the call with "technical
    problem, transferring you". Seen on a real call: the carry-over handed over a burst of
    room noise, recognition rejected it, and the caller lost the conversation.
    """
    _reader, _writer, _registry, session = build(BrokenChatModel())

    class Counting:
        def __init__(self) -> None:
            self.asked = 0

        async def open(self, language: str) -> None:
            return None

        async def transcribe(self, pcm16k: bytes):
            self.asked += 1
            raise AssertionError("a fragment must never reach recognition")

        async def close(self) -> None:
            return None

    session._stt = Counting()
    fragment = Utterance(pcm8k=b"\x00\x01" * 1200, ended_by="silence")  # 300 ms
    assert fragment.duration_ms < 600

    assert await session._decide(fragment) is None
    assert session._stt.asked == 0


LONG_GREETING = dict(PROMPTS, greeting="salom " * 60)  # seconds of speech from the fake


async def test_the_greeting_plays_to_the_end_however_loud_the_room_is():
    """The behaviour most likely to be demonstrated live, and it had no test at all.

    A caller hearing this service for the first time is told what it is and how to use
    it. In a hall the room itself was cutting that off within half a second, every call,
    so nobody ever learned where they had got through to.
    """
    call_id = str(uuid.uuid4())
    reader, _writer, _registry, session = build(BrokenChatModel(), prompts=LONG_GREETING)
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))
    task = asyncio.create_task(session.run())
    # Only once the greeting is audible: before that there is nothing to interrupt, and a
    # test that shouts into silence proves nothing either way.
    await wait_for(lambda: session._out.speaking, timeout=5)

    for _ in range(60):
        reader.feed_data(encode(0x10, SPEECH))
        await asyncio.sleep(0.005)

    assert session.state == "greeting", "the room cut the greeting off"
    reader.feed_eof()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=5)


async def test_a_caller_may_still_interrupt_the_greeting_when_it_is_allowed():
    """The same greeting, the same frames, the same moment — one setting apart, so the
    test above is measuring the setting and not the silence before the greeting starts."""
    call_id = str(uuid.uuid4())
    talkative = replace(CONFIG, interruptible_greeting=True)
    reader, _writer, _registry, session = build(
        BrokenChatModel(), prompts=LONG_GREETING, config=talkative
    )
    reader.feed_data(encode(0x01, uuid.UUID(call_id).bytes))
    task = asyncio.create_task(session.run())
    await wait_for(lambda: session._out.speaking, timeout=5)

    for _ in range(60):
        reader.feed_data(encode(0x10, SPEECH))
        await asyncio.sleep(0.005)

    assert session.state != "greeting", "the caller could not interrupt when allowed to"
    reader.feed_eof()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=5)


async def test_a_telephone_s_g711_is_decoded_before_anything_listens_to_it():
    """The decoding table has its own test; this is the wiring, which is where the bug
    was. Twenty seconds of a clearly spoken question produced not one turn because the
    bridge read G.711 bytes as 16-bit samples — a roar with the voice buried in it.

    The frames handed to the detectors are what matters, so that is what is measured:
    sent at RMS 8000, they must arrive at RMS 8000. Undecoded they arrive at twice that,
    and the silence between words arrives louder than the words.
    """
    import numpy as np

    from eduvoice.audio import ULAW_TO_LINEAR

    seen: list[float] = []

    def watch(frame: bytes) -> bool:
        block = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        seen.append(float(np.sqrt(np.mean(block * block))))
        return False

    call_id = str(uuid.uuid4())
    _reader, _writer, registry, session = build(BrokenChatModel())
    registry.start(call_id).audio_format = "ulaw"
    session.call_id = call_id
    session._state = "listening"
    session._speech = SpeechDetector(CONFIG, speech_test=watch)

    to_ulaw = {int(v): i for i, v in enumerate(ULAW_TO_LINEAR)}
    levels = sorted(to_ulaw)
    nearest = lambda want: to_ulaw[min(levels, key=lambda v: abs(v - want))]  # noqa: E731

    await session._on_incoming_audio(bytes([nearest(8000), nearest(-8000)] * 80))
    await session._on_incoming_audio(bytes([nearest(0)] * 160))

    assert len(seen) == 2, f"{len(seen)} frames reached the detector, expected 2"
    spoken, silent = seen
    assert 7000 < spoken < 9000, f"a word arrived at RMS {spoken:.0f}, not the 8000 sent"
    assert silent < 100, f"silence arrived at RMS {silent:.0f}"
    await session.aclose()
