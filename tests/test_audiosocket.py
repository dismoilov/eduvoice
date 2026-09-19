import asyncio
import time
import uuid

import pytest

from eduvoice.audio import tone
from eduvoice.audiosocket import (
    FRAME_BYTES,
    KIND_AUDIO,
    KIND_HANGUP,
    KIND_UUID,
    AudioOut,
    Frame,
    encode,
    read_frames,
)
from eduvoice.interfaces import SAMPLE_RATE_TELEPHONY


class FakeWriter:
    """Captures what the bridge would send to Asterisk."""

    def __init__(self) -> None:
        self.messages: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.messages.append(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def frames(self) -> list[bytes]:
        return [m[3:] for m in self.messages]


def make_reader(*messages: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for message in messages:
        reader.feed_data(message)
    reader.feed_eof()
    return reader


def test_encode_builds_a_header_with_big_endian_length():
    assert encode(KIND_AUDIO, b"\x01\x02") == b"\x10\x00\x02\x01\x02"
    assert encode(KIND_HANGUP) == b"\x00\x00\x00"


def test_encode_rejects_oversized_payload():
    with pytest.raises(ValueError, match="65535"):
        encode(KIND_AUDIO, b"\x00" * 70000)


def test_uuid_frame_is_decoded():
    call_id = uuid.uuid4()
    frame = Frame(KIND_UUID, call_id.bytes)
    assert frame.call_id() == str(call_id)


def test_call_id_rejects_other_frame_kinds():
    with pytest.raises(ValueError, match="not a UUID frame"):
        Frame(KIND_AUDIO, b"\x00" * 320).call_id()


async def test_read_frames_parses_a_call():
    call_id = uuid.uuid4()
    audio = b"\x01" * FRAME_BYTES
    reader = make_reader(
        encode(KIND_UUID, call_id.bytes),
        encode(KIND_AUDIO, audio),
        encode(KIND_HANGUP),
        encode(KIND_AUDIO, audio),  # must be ignored: the call already ended
    )

    frames = [frame async for frame in read_frames(reader)]

    assert [f.kind for f in frames] == [KIND_UUID, KIND_AUDIO, KIND_HANGUP]
    assert frames[0].call_id() == str(call_id)
    assert frames[1].payload == audio


async def test_read_frames_survives_a_truncated_message():
    reader = make_reader(encode(KIND_AUDIO, b"\x01" * 100)[:-10])  # cut mid-payload
    assert [frame async for frame in read_frames(reader)] == []


async def test_audio_out_sends_320_byte_frames_in_real_time():
    writer = FakeWriter()
    out = AudioOut(writer)  # type: ignore[arg-type]
    out.start()

    out.write(tone(1000, SAMPLE_RATE_TELEPHONY))  # one second of audio
    started = time.monotonic()
    await out.wait_drained()
    elapsed = time.monotonic() - started
    await out.aclose()

    assert out.frames_sent == 50
    assert {len(f) for f in writer.frames()} == {FRAME_BYTES}
    # The property that matters is "not sent as a burst"; the upper bound is loose so a
    # busy machine does not fail the suite.
    assert 0.9 < elapsed < 1.6, f"pacing off: {elapsed:.2f}s for 1s of audio"


async def test_audio_out_pads_the_last_partial_frame():
    writer = FakeWriter()
    out = AudioOut(writer)  # type: ignore[arg-type]
    out.start()

    out.write(b"\x01" * 100)
    await out.wait_drained()
    await out.aclose()

    assert out.frames_sent == 1
    assert writer.frames()[0] == b"\x01" * 100 + b"\x00" * (FRAME_BYTES - 100)


async def test_clear_stops_playback_immediately():
    writer = FakeWriter()
    out = AudioOut(writer)  # type: ignore[arg-type]
    out.start()

    out.write(tone(2000, SAMPLE_RATE_TELEPHONY))  # two seconds queued
    await asyncio.sleep(0.1)
    sent_before = out.frames_sent
    out.clear()  # barge-in
    assert not out.speaking
    await asyncio.sleep(0.1)
    sent_after = out.frames_sent
    await out.aclose()

    assert sent_after == sent_before, "audio kept playing after clear()"
    assert sent_before < 25, "clear() came too late to be a barge-in"


async def test_new_audio_after_clear_is_played():
    writer = FakeWriter()
    out = AudioOut(writer)  # type: ignore[arg-type]
    out.start()

    out.write(tone(1000, SAMPLE_RATE_TELEPHONY))
    await asyncio.sleep(0.05)
    out.clear()
    out.write(b"\x02" * FRAME_BYTES)
    await out.wait_drained()
    await out.aclose()

    assert writer.frames()[-1] == b"\x02" * FRAME_BYTES


async def test_a_socket_that_broke_on_our_last_write_is_a_hangup_not_a_crash():
    """Asterisk closes the socket the instant our goodbye ends. The transport records the
    failed write and the *reader* raises it on its next call — after cleanup was done.
    Uncaught, that came out of run() as "call crashed", and the crash handler then turned
    a "hangup" into "operator" for someone who had just said goodbye."""
    from eduvoice.audiosocket import read_frames

    class Broken:
        async def readexactly(self, n: int) -> bytes:
            raise BrokenPipeError(32, "Broken pipe")

    frames = [frame async for frame in read_frames(Broken())]  # type: ignore[arg-type]
    assert frames == []


def test_a_finished_call_keeps_its_decision_after_a_late_exception():
    from eduvoice.main import send_to_a_person_unless_decided
    from eduvoice.registry import CallRegistry

    calls = CallRegistry()
    calls.start("done")
    calls.set_next("done", "hangup")
    calls.finish("done")
    send_to_a_person_unless_decided(calls, "done")
    assert calls.next_action("done") == "hangup", "a goodbye was turned into a transfer"

    calls.start("mid-call")
    send_to_a_person_unless_decided(calls, "mid-call")
    assert calls.next_action("mid-call") == "operator", "a real crash must still reach a person"
