import numpy as np
import pytest

from eduvoice.audio import (
    BYTES_PER_SAMPLE,
    FrameSplitter,
    StreamResampler,
    duration_ms,
    frame_bytes,
    resample,
    rms_dbfs,
    silence,
    tone,
)
from eduvoice.interfaces import SAMPLE_RATE_STT, SAMPLE_RATE_TELEPHONY, SAMPLE_RATE_TTS


def test_frame_size_is_320_bytes_for_telephony():
    assert frame_bytes(SAMPLE_RATE_TELEPHONY) == 320
    assert frame_bytes(SAMPLE_RATE_STT) == 640
    assert frame_bytes(SAMPLE_RATE_TTS) == 960


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (SAMPLE_RATE_TELEPHONY, SAMPLE_RATE_STT),  # to VoiceLab speech-to-text
        (SAMPLE_RATE_TTS, SAMPLE_RATE_TELEPHONY),  # from VoiceLab text-to-speech to the call
        (SAMPLE_RATE_STT, SAMPLE_RATE_TTS),
    ],
)
def test_resample_keeps_duration(src, dst):
    source = tone(1000, src)
    converted = resample(source, src, dst)
    assert duration_ms(converted, dst) == pytest.approx(1000, abs=1)


def test_resample_roundtrip_keeps_duration_and_signal():
    source = tone(500, SAMPLE_RATE_TELEPHONY)
    there = resample(source, SAMPLE_RATE_TELEPHONY, SAMPLE_RATE_STT)
    back = resample(there, SAMPLE_RATE_STT, SAMPLE_RATE_TELEPHONY)
    assert duration_ms(back, SAMPLE_RATE_TELEPHONY) == pytest.approx(500, abs=1)
    # loudness must survive the round trip (no silence, no clipping)
    assert rms_dbfs(back) == pytest.approx(rms_dbfs(source), abs=1.0)


def test_resample_is_a_noop_for_equal_rates():
    source = tone(20, SAMPLE_RATE_TELEPHONY)
    assert resample(source, SAMPLE_RATE_TELEPHONY, SAMPLE_RATE_TELEPHONY) is source


def test_silence_is_digital_silence():
    quiet = silence(20, SAMPLE_RATE_TELEPHONY)
    assert len(quiet) == 320
    assert rms_dbfs(quiet) == float("-inf")


def test_splitter_cuts_exactly_50_frames_per_second():
    splitter = FrameSplitter(SAMPLE_RATE_TELEPHONY)
    frames = splitter.push(tone(1000, SAMPLE_RATE_TELEPHONY))
    assert len(frames) == 50
    assert {len(f) for f in frames} == {320}
    assert splitter.pending == 0


def test_splitter_keeps_the_tail_until_more_audio_arrives():
    splitter = FrameSplitter(SAMPLE_RATE_TELEPHONY)
    assert splitter.push(b"\x01" * 100) == []  # not a full frame yet
    assert splitter.pending == 100
    frames = splitter.push(b"\x02" * 300)
    assert len(frames) == 1
    assert frames[0][:100] == b"\x01" * 100  # nothing was dropped or reordered
    assert splitter.pending == 80


def test_splitter_flush_pads_the_remainder():
    splitter = FrameSplitter(SAMPLE_RATE_TELEPHONY)
    splitter.push(b"\x01" * 80)
    tail = splitter.flush()
    assert tail is not None
    assert len(tail) == 320
    assert tail[80:] == b"\x00" * 240
    assert splitter.flush() is None


def test_splitter_clear_drops_buffered_audio():
    splitter = FrameSplitter(SAMPLE_RATE_TELEPHONY)
    splitter.push(b"\x01" * 100)
    splitter.clear()
    assert splitter.pending == 0


def test_stream_resampler_matches_a_whole_buffer_conversion():
    """Chunked synthesis must sound the same as one buffer: no clicks at the seams."""
    source = tone(500, 24_000, frequency=300)
    whole = resample(source, 24_000, 8_000)

    converter = StreamResampler(24_000, 8_000)
    chunks = [source[i : i + 977] for i in range(0, len(source), 977)]  # odd sizes on purpose
    streamed = b"".join(converter.push(c) for c in chunks) + converter.flush()

    assert abs(len(streamed) - len(whole)) <= 4 * BYTES_PER_SAMPLE
    a = np.frombuffer(whole[: len(streamed)], dtype="<i2").astype(float)
    b = np.frombuffer(streamed[: len(whole)], dtype="<i2").astype(float)
    # Same signal, sample for sample (the filter state is carried between chunks).
    assert float(np.max(np.abs(a - b))) < 600


def test_stream_resampler_survives_chunks_split_mid_sample():
    """A provider may split a chunk between the two bytes of one sample."""
    converter = StreamResampler(24_000, 8_000)
    source = tone(200, 24_000)

    converter.push(source[:1])  # half a sample
    rest = b"".join(converter.push(source[i : i + 1]) for i in range(1, len(source)))
    out = rest + converter.flush()

    assert len(out) % BYTES_PER_SAMPLE == 0
    assert duration_ms(out, 8_000) == pytest.approx(200, abs=1)


def test_resample_ignores_a_stray_odd_byte():
    """Never raise on a malformed buffer: a click is better than a dropped call."""
    assert len(resample(b"\x00" * 481, 24_000, 8_000)) > 0


def test_a_telephone_speaks_g711_and_the_bridge_must_understand_it():
    """A real telephone negotiates G.711 — one byte per sample. Read as 16-bit audio it is
    a constant full-scale roar: the detector hears speech without pause, no phrase ever
    ends, and nothing is ever recognised. Measured on a real call: every second at RMS
    16000 with peaks at 32767, and 20.8 seconds of talking produced not one turn.

    `Local/` test channels are linear, which is exactly why this never showed up in
    testing.
    """
    import struct

    from eduvoice.audio import from_g711

    # 0xFF is silence in mu-law, 0x00 its largest negative value.
    assert struct.unpack("<h", from_g711(b"\xff"))[0] == 0
    assert struct.unpack("<h", from_g711(b"\x00"))[0] == -32124
    assert struct.unpack("<h", from_g711(b"\xd5", alaw=True))[0] == 8

    # One byte in, two bytes out — 20 ms of telephone audio is 160 bytes, not 320.
    decoded = from_g711(b"\xff" * 160)
    assert len(decoded) == 320

    # Silence decodes to silence, which is the whole point: it lets a phrase end.
    assert set(decoded) == {0}
