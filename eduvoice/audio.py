"""PCM helpers: resampling and 20 ms framing.

All audio in the bridge is raw PCM signed 16-bit little-endian, mono.
Sample rates differ per hop (see eduvoice.interfaces), so every function takes the rate
explicitly — there is no "default" rate anywhere.
"""

from __future__ import annotations

import math

import numpy as np
import soxr

FRAME_MS = 20
BYTES_PER_SAMPLE = 2


def _g711_table(alaw: bool) -> np.ndarray:
    """Lookup from one G.711 byte to a signed 16-bit sample."""
    values = np.arange(256, dtype=np.int32)
    if alaw:
        code = values ^ 0x55
        mantissa, exponent = code & 0x0F, (code & 0x70) >> 4
        magnitude = np.where(
            exponent == 0, (mantissa << 4) + 8, ((mantissa << 4) + 0x108) << (exponent - 1)
        )
        return np.where(code & 0x80, magnitude, -magnitude).astype(np.int16)
    code = ~values & 0xFF
    mantissa, exponent = code & 0x0F, (code & 0x70) >> 4
    magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return np.where(code & 0x80, -magnitude, magnitude).astype(np.int16)


ULAW_TO_LINEAR = _g711_table(alaw=False)
ALAW_TO_LINEAR = _g711_table(alaw=True)


def from_g711(data: bytes, alaw: bool = False) -> bytes:
    """One byte per sample in, two bytes per sample out.

    Asterisk hands the AudioSocket channel whatever format the call negotiated, and a real
    telephone negotiates G.711 — one byte per sample. Read as 16-bit audio that is a
    constant full-scale roar: the voice detector hears speech without pause, no phrase
    ever ends, and nothing is ever recognised. Test calls made with `Local/` channels are
    linear, which is why they never showed it.
    """
    table = ALAW_TO_LINEAR if alaw else ULAW_TO_LINEAR
    return table[np.frombuffer(data, dtype=np.uint8)].tobytes()


def frame_bytes(sample_rate: int, frame_ms: int = FRAME_MS) -> int:
    """Size of one frame in bytes: 320 for 8 kHz / 20 ms."""
    samples = sample_rate * frame_ms // 1000
    return samples * BYTES_PER_SAMPLE


def duration_ms(pcm: bytes, sample_rate: int) -> float:
    return len(pcm) / BYTES_PER_SAMPLE / sample_rate * 1000


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample a complete PCM s16le mono buffer. Returns the input unchanged when rates match.

    For a stream arriving in chunks use StreamResampler: resampling chunks one by one with
    this function restarts the filter every time, which clicks at every chunk boundary.
    """
    if src_rate == dst_rate or not pcm:
        return pcm
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % BYTES_PER_SAMPLE], dtype="<i2")
    converted = soxr.resample(samples, src_rate, dst_rate, quality="VHQ")
    return np.asarray(converted, dtype="<i2").tobytes()


class StreamResampler:
    """Resamples a chunked stream while keeping the filter state between chunks.

    Used for text-to-speech audio (24 kHz -> 8 kHz), which arrives in many small chunks:
    one continuous filter means no clicks at the seams and no drift from dropped tails.
    An odd trailing byte is carried over to the next chunk instead of raising.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self._src_rate = src_rate
        self._dst_rate = dst_rate
        self._tail = b""
        self._stream = (
            None
            if src_rate == dst_rate
            else soxr.ResampleStream(src_rate, dst_rate, 1, dtype="int16", quality="VHQ")
        )

    def push(self, pcm: bytes) -> bytes:
        if self._stream is None:
            return pcm
        return self._convert(pcm, last=False)

    def flush(self) -> bytes:
        """Last chunk: pushes the filter tail out so the final syllable is not lost."""
        if self._stream is None:
            return b""
        return self._convert(b"", last=True)

    def _convert(self, pcm: bytes, *, last: bool) -> bytes:
        assert self._stream is not None
        data = self._tail + pcm
        usable = len(data) - len(data) % BYTES_PER_SAMPLE
        self._tail = data[usable:]
        samples = np.frombuffer(data[:usable], dtype="<i2")
        converted = self._stream.resample_chunk(samples, last=last)
        return np.asarray(converted, dtype="<i2").tobytes()


def silence(ms: float, sample_rate: int) -> bytes:
    return b"\x00" * (int(sample_rate * ms / 1000) * BYTES_PER_SAMPLE)


def tone(ms: float, sample_rate: int, frequency: float = 440.0, amplitude: float = 0.25) -> bytes:
    """Test/placeholder signal (used by the fake text-to-speech provider)."""
    count = int(sample_rate * ms / 1000)
    t = np.arange(count, dtype=np.float64) / sample_rate
    wave = np.sin(2 * math.pi * frequency * t) * amplitude * 32767
    return wave.astype("<i2").tobytes()


def rms_dbfs(pcm: bytes) -> float:
    """Loudness in dBFS; -inf for digital silence. Useful in logs when tuning the VAD."""
    if not pcm:
        return float("-inf")
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    rms = math.sqrt(float(np.mean(samples**2)))
    return 20 * math.log10(rms / 32768) if rms > 0 else float("-inf")


class FrameSplitter:
    """Cuts a byte stream into fixed-size frames, keeping the tail for the next call.

    Nothing is ever dropped: bytes that do not fill a frame wait for more audio.
    """

    def __init__(self, sample_rate: int, frame_ms: int = FRAME_MS) -> None:
        self.size = frame_bytes(sample_rate, frame_ms)
        self._buffer = bytearray()

    def push(self, pcm: bytes) -> list[bytes]:
        self._buffer.extend(pcm)
        frames: list[bytes] = []
        while len(self._buffer) >= self.size:
            frames.append(bytes(self._buffer[: self.size]))
            del self._buffer[: self.size]
        return frames

    def flush(self, pad: bool = True) -> bytes | None:
        """Returns the remainder (padded with silence by default) and clears the buffer."""
        if not self._buffer:
            return None
        tail = bytes(self._buffer)
        self._buffer.clear()
        if pad and len(tail) < self.size:
            tail += b"\x00" * (self.size - len(tail))
        return tail

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()
