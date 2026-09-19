import asyncio
import uuid
from dataclasses import replace

import pytest

from eduvoice.audiosocket import FRAME_BYTES, KIND_AUDIO, KIND_HANGUP, KIND_UUID, encode
from eduvoice.brain import Brain
from eduvoice.config import settings as base_settings
from eduvoice.fakes import FakeChatModel, FakeSpeechToText, FakeTextToSpeech
from eduvoice.prompts import PromptLibrary
from eduvoice.registry import CallRegistry
from eduvoice.session import CallSession
from eduvoice.vad import BargeInDetector, SpeechDetector

SPEECH = b"\x11" * FRAME_BYTES
QUIET = b"\x00" * FRAME_BYTES

PROMPTS = {
    "greeting": "salom",
    "filler": "bir daqiqa",
    "still_checking": "hali tekshiryapman",
    "reprompt": "savolingizni ayting",
    "repeat_please": "qaytaring",
    "not_understood": "tushunmadim",
    "out_of_scope": "faqat taʼlim",
    "transfer": "operatorga ulayman",
    "tech_problem": "texnik nosozlik",
    "goodbye": "rahmat",
}

CONFIG = replace(
    base_settings,
    speech_start_frames=2,
    speech_start_window=3,
    silence_end_frames=5,
    silence_reprompt_s=0.4,  # 20 frames of silence
    barge_in_guard_ms=0,
    barge_in_frames=3,
    barge_in_window=5,
    filler_after_s=10,  # long enough not to interfere with the tests
)


class StubWriter:
    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.messages.append(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    def kinds(self) -> list[int]:
        return [m[0] for m in self.messages]


def is_speech_frame(frame: bytes) -> bool:
    """Speech is decided by frame content, so the order of arrival cannot skew a test."""
    return frame[:1] == b"\x11"


def build(stt_texts: list[str], faq: dict[str, str] | None = None):
    reader = asyncio.StreamReader()
    writer = StubWriter()
    registry = CallRegistry()
    session = CallSession(
        reader,
        writer,  # type: ignore[arg-type]
        stt=FakeSpeechToText(stt_texts, latency_ms=10),
        tts=FakeTextToSpeech(first_chunk_ms=10, ms_per_char=2),
        brain=Brain(FakeChatModel(latency_ms=10), faq=faq or {"stipend": "stipendiya javobi"}),
        prompts=PromptLibrary(PROMPTS),
        registry=registry,
        config=CONFIG,
        speech_detector=SpeechDetector(CONFIG, speech_test=is_speech_frame),
        barge_detector=BargeInDetector(CONFIG, speech_test=is_speech_frame),
    )
    return reader, writer, registry, session


def feed(reader: asyncio.StreamReader, frames: list[bytes]) -> None:
    for frame in frames:
        reader.feed_data(encode(KIND_AUDIO, frame))


def start_call(reader: asyncio.StreamReader, call_id: str) -> None:
    reader.feed_data(encode(KIND_UUID, uuid.UUID(call_id).bytes))


async def ask(reader, session, frames: int = 40) -> None:
    """Waits for the bot to finish talking, then says something and goes quiet.

    Forty frames is 800 ms: anything under 600 ms is dropped as a fragment, because the
    recogniser refuses audio shorter than half a second.
    """
    await wait_for(lambda: session.state == "listening")
    feed(reader, [SPEECH] * frames + [QUIET] * 10)


async def wait_for(condition, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_question_is_answered_and_recorded():
    call_id = str(uuid.uuid4())
    reader, writer, registry, session = build(["stipendiya qanday olinadi"])
    start_call(reader, call_id)

    task = asyncio.create_task(session.run())
    await ask(reader, session)
    await wait_for(lambda: bool(registry.get(call_id) and registry.get(call_id).turns))
    reader.feed_eof()
    await task

    record = registry.get(call_id)
    turn = record.turns[0]
    assert turn.question == "stipendiya qanday olinadi"
    assert turn.intent == "faq"
    assert turn.answer == "stipendiya javobi"
    assert turn.latency_ms["total_ms"] > 0
    assert KIND_AUDIO in writer.kinds(), "the caller heard nothing"

    # What the supervisor panel needs: which rule was applied and where to find the
    # question in the recording.
    assert turn.action == "faq"
    assert turn.faq_id == "stipend"
    assert turn.at_ms >= 0
    assert record.recording.endswith(f"{call_id}.wav")


async def test_operator_request_hands_the_call_to_a_human():
    call_id = str(uuid.uuid4())
    reader, writer, registry, session = build(["operator bilan gaplashmoqchiman"])
    start_call(reader, call_id)

    task = asyncio.create_task(session.run())
    await ask(reader, session)
    await wait_for(
        lambda: registry.next_action(call_id) == "operator" and KIND_HANGUP in writer.kinds()
    )
    reader.feed_eof()
    await task

    assert registry.next_action(call_id) == "operator"
    assert writer.kinds()[-1] == KIND_HANGUP, "the bridge must close the socket for the dialplan"
    # The panel must show what the caller actually heard, not an empty answer.
    assert registry.get(call_id).turns[-1].answer == PROMPTS["transfer"]


async def test_goodbye_ends_the_call_with_hangup():
    call_id = str(uuid.uuid4())
    reader, _writer, registry, session = build(["rahmat, xayr"])
    start_call(reader, call_id)

    task = asyncio.create_task(session.run())
    await ask(reader, session)
    await wait_for(lambda: registry.next_action(call_id) == "hangup")
    reader.feed_eof()
    await task

    assert registry.next_action(call_id) == "hangup"
    assert registry.get(call_id).turns[-1].answer == PROMPTS["goodbye"]


async def test_silence_leads_to_a_reprompt_then_goodbye():
    call_id = str(uuid.uuid4())
    reader, _writer, registry, session = build(["hech narsa"])
    start_call(reader, call_id)

    task = asyncio.create_task(session.run())
    await wait_for(lambda: session.state == "listening")
    feed(reader, [QUIET] * 25)  # silence -> reprompt
    await wait_for(lambda: PROMPTS["reprompt"] in session._tts.spoken)
    await wait_for(lambda: session.state == "listening")
    feed(reader, [QUIET] * 25)  # silence again -> goodbye
    await wait_for(lambda: registry.next_action(call_id) == "hangup")
    reader.feed_eof()
    await task

    spoken = session._tts.spoken
    assert PROMPTS["reprompt"] in spoken
    assert PROMPTS["goodbye"] in spoken
    assert registry.next_action(call_id) == "hangup"


async def test_unknown_call_still_gets_a_safe_default():
    """A call the dialplan never announced must still end up with an operator."""
    registry = CallRegistry()
    assert registry.next_action(str(uuid.uuid4())) == "operator"


@pytest.mark.parametrize("hangup_first", [True, False])
async def test_caller_hangup_is_handled(hangup_first: bool):
    call_id = str(uuid.uuid4())
    reader, writer, registry, session = build(["salom"])
    start_call(reader, call_id)
    if not hangup_first:
        for _ in range(5):
            reader.feed_data(encode(KIND_AUDIO, QUIET))
    reader.feed_data(encode(KIND_HANGUP))

    await asyncio.wait_for(session.run(), timeout=5)

    record = registry.get(call_id)
    assert record is not None and record.ended_at is not None
    assert writer.closed
