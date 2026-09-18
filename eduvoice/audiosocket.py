"""AudioSocket protocol (Asterisk <-> bridge).

Wire format of every message:

    1 byte  kind
    2 bytes payload length, big-endian
    N bytes payload

Asterisk 20.4 sends audio only: PCM s16le mono 8 kHz in 20 ms frames (320 bytes).
DTMF and channel variables never arrive here — that is why the dialplan passes call
data over HTTP (eduvoice.control_api).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from eduvoice.audio import frame_bytes
from eduvoice.interfaces import SAMPLE_RATE_TELEPHONY

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF

log = logging.getLogger("eduvoice.audiosocket")

HEADER = struct.Struct(">BH")
FRAME_MS = 20
FRAME_BYTES = frame_bytes(SAMPLE_RATE_TELEPHONY, FRAME_MS)  # 320


@dataclass(frozen=True, slots=True)
class Frame:
    kind: int
    payload: bytes

    @property
    def is_audio(self) -> bool:
        return self.kind == KIND_AUDIO

    @property
    def is_hangup(self) -> bool:
        return self.kind == KIND_HANGUP

    def call_id(self) -> str:
        """UUID of the call, for the 0x01 frame Asterisk sends first."""
        if self.kind != KIND_UUID:
            raise ValueError(f"frame kind {self.kind:#04x} is not a UUID frame")
        return str(uuid.UUID(bytes=self.payload))


def encode(kind: int, payload: bytes = b"") -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("AudioSocket payload must fit in 65535 bytes")
    return HEADER.pack(kind, len(payload)) + payload


async def read_frames(reader: asyncio.StreamReader) -> AsyncIterator[Frame]:
    """Yields frames until Asterisk hangs up or the connection drops."""
    while True:
        try:
            header = await reader.readexactly(HEADER.size)
            kind, length = HEADER.unpack(header)
            payload = await reader.readexactly(length) if length else b""
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        yield Frame(kind, payload)
        if kind == KIND_HANGUP:
            return


class AudioOut:
    """Sends audio to the call at exactly one 20 ms frame per 20 ms.

    Asterisk plays what it receives as it arrives, so the pace must match real time:
    sending faster makes the caller hear nothing but a burst, slower makes gaps.
    `clear()` drops everything still queued — that is how barge-in stops the bot mid-word.
    """

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._buffer = bytearray()
        self._has_data = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._failed = False
        self.frames_sent = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="audio-out")

    def write(self, pcm8k: bytes) -> None:
        if self._closed or not pcm8k:
            return
        self._buffer.extend(pcm8k)
        self._drained.clear()
        self._has_data.set()

    def clear(self) -> None:
        """Barge-in: forget queued audio immediately."""
        self._buffer.clear()
        self._has_data.clear()
        self._drained.set()

    @property
    def speaking(self) -> bool:
        return bool(self._buffer)

    @property
    def queued_ms(self) -> float:
        return len(self._buffer) / (SAMPLE_RATE_TELEPHONY * 2) * 1000

    @property
    def failed(self) -> bool:
        """True when the socket died: the call is over, nothing else will be heard."""
        return self._failed

    async def wait_drained(self, timeout: float | None = None) -> bool:
        """Waits until everything queued has been sent. False on timeout.

        A timeout is always passed by the session: without one a stalled socket would
        leave the call hanging in `speaking` forever.
        """
        if timeout is None:
            await self._drained.wait()
            return True
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def aclose(self) -> None:
        self._closed = True
        self.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while not self._closed:
                await self._has_data.wait()
                deadline = loop.time()
                while self._buffer and not self._closed:
                    chunk = bytes(self._buffer[:FRAME_BYTES])
                    del self._buffer[: len(chunk)]
                    if len(chunk) < FRAME_BYTES:  # pad the tail of an utterance
                        chunk += b"\x00" * (FRAME_BYTES - len(chunk))
                    self._writer.write(encode(KIND_AUDIO, chunk))
                    await self._writer.drain()
                    self.frames_sent += 1
                    deadline += FRAME_MS / 1000
                    delay = deadline - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    else:
                        deadline = loop.time()  # we fell behind; resync instead of bursting
                self._has_data.clear()
                self._drained.set()
        except (OSError, RuntimeError) as exc:
            # The caller hung up mid-answer: stop, but wake everyone waiting for the
            # audio to drain, otherwise the session would sit in `speaking` forever.
            log.info("audio output stopped: %s", exc)
            self._failed = True
            self._closed = True
            self._buffer.clear()
            self._drained.set()


ConnectionHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def serve(handler: ConnectionHandler, host: str, port: int) -> asyncio.Server:
    """Starts the AudioSocket server; one connection == one call."""
    return await asyncio.start_server(handler, host, port)
