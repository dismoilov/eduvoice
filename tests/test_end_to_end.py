"""The whole bridge over real sockets, with the real content files.

This is the test that proves the demo works: it starts the AudioSocket server and the
control API exactly as `eduvoice.main` does, plays a call through a TCP connection the way
Asterisk does, and then asks the control API what the dialplan should do next.

Only the speech providers are fakes — everything else is production code.
"""

import asyncio
import uuid

import pytest

from eduvoice.audiosocket import KIND_AUDIO, KIND_HANGUP, KIND_UUID, encode
from eduvoice.config import settings
from eduvoice.content import Faq
from eduvoice.control_api import serve_control_api
from eduvoice.main import make_call_handler
from eduvoice.prompts import PromptLibrary
from eduvoice.registry import CallRegistry

# Every phrase the dialogue code can ask for. If one is missing from prompts.yaml the
# caller would hear nothing at that moment, so the content is part of the contract.
REQUIRED_PROMPTS = [
    "greeting",
    "filler",
    "reprompt",
    "repeat_please",
    "not_understood",
    "out_of_scope",
    "transfer",
    "tech_problem",
    "goodbye",
]

FRAME = 320
SPEECH = bytes(range(256)) * 2  # noisy enough for webrtcvad to call it speech
QUIET = b"\x00" * FRAME


def test_the_content_files_contain_every_phrase_the_code_uses():
    prompts = PromptLibrary.load(settings.content_dir)
    missing = [p for p in REQUIRED_PROMPTS if p not in prompts.ids()]
    assert not missing, f"prompts.yaml is missing: {missing}"


def test_every_faq_answer_is_speakable():
    """Answers are read out loud: no links, no bullet lists, no wall of text."""
    for faq_id, answer in Faq.load(settings.content_dir).answers().items():
        assert answer, f"{faq_id} has no answer"
        assert "http" not in answer, f"{faq_id} contains a link, which cannot be spoken"
        assert len(answer) < 600, f"{faq_id} is too long to be read out on the phone"


@pytest.fixture
async def bridge(tmp_path, monkeypatch):
    """The real servers on random ports, with a temporary prompt cache and call log."""
    # Hermetic on purpose: a developer's .env may point the bridge at the real provider.
    monkeypatch.setattr(settings, "provider", "fake")
    monkeypatch.setattr(settings, "call_log_dir", tmp_path / "logs")
    monkeypatch.setattr(settings, "audio_dir", tmp_path / "audio")
    monkeypatch.setenv("EDUVOICE_FAKE_STT", "operator bilan gaplashmoqchiman")

    registry = CallRegistry()
    handler = make_call_handler(
        Faq.load(settings.content_dir),
        PromptLibrary.load(settings.content_dir, tmp_path / "audio"),
    )
    monkeypatch.setattr("eduvoice.main.registry", registry)
    audio = await asyncio.start_server(handler, "127.0.0.1", 0)
    control = await serve_control_api(registry, "127.0.0.1", 0, health={"provider": "fake"})
    yield registry, audio.sockets[0].getsockname()[1], control.sockets[0].getsockname()[1]
    audio.close()
    control.close()
    await audio.wait_closed()
    await control.wait_closed()


async def http_get(port: int, path: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    raw = (await reader.read()).decode()
    writer.close()
    return raw.split("\r\n\r\n", 1)[1]


async def test_a_call_asking_for_an_operator_is_handed_to_a_human(bridge):
    registry, audio_port, control_port = bridge
    call_id = str(uuid.uuid4())

    # The dialplan announces the call before dialling the bridge.
    reader, writer = await asyncio.open_connection("127.0.0.1", control_port)
    body = "caller=998901234567"
    writer.write(
        f"POST /calls/{call_id}/start HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: {len(body)}\r\n"
        f"\r\n{body}".encode()
    )
    await writer.drain()
    await reader.read()
    writer.close()

    # Asterisk connects and sends the UUID, then audio in real time.
    call_reader, call_writer = await asyncio.open_connection("127.0.0.1", audio_port)
    call_writer.write(encode(KIND_UUID, uuid.UUID(call_id).bytes))
    await call_writer.drain()

    heard_hangup = False
    last_audio_at = 0.0

    async def talk() -> None:
        """Hear the greeting out, then speak, then wait for the answer.

        Waiting for the line to go quiet rather than counting frames: the greeting cannot
        be interrupted any more, and how long it lasts depends on its text. This used to
        pass by accident — webrtcvad calls even pure silence speech, so the frames sent
        while the bot greeted cut the greeting short and the rest of the test fitted.
        """

        async def quiet_for(seconds: float) -> None:
            while True:
                call_writer.write(encode(KIND_AUDIO, QUIET))
                await call_writer.drain()
                await asyncio.sleep(0.02)
                if heard_hangup:
                    return
                if last_audio_at and asyncio.get_running_loop().time() - last_audio_at > seconds:
                    return

        await quiet_for(0.4)  # the greeting has finished playing
        for _ in range(40):
            call_writer.write(encode(KIND_AUDIO, SPEECH))
            await call_writer.drain()
            await asyncio.sleep(0.02)
        for _ in range(310):
            call_writer.write(encode(KIND_AUDIO, QUIET))
            await call_writer.drain()
            await asyncio.sleep(0.02)
            if heard_hangup:
                return

    async def listen() -> None:
        nonlocal heard_hangup, last_audio_at
        while True:
            header = await call_reader.readexactly(3)
            kind = header[0]
            length = int.from_bytes(header[1:3], "big")
            if length:
                await call_reader.readexactly(length)
            if kind == KIND_AUDIO:
                last_audio_at = asyncio.get_running_loop().time()
            if kind == KIND_HANGUP:
                heard_hangup = True
                return

    async with asyncio.timeout(30):
        await asyncio.gather(talk(), listen())
    call_writer.close()

    assert heard_hangup, "the bridge never handed the call back to the dialplan"
    assert await http_get(control_port, f"/calls/{call_id}/next") == "operator"

    record = registry.get(call_id)
    assert record is not None
    assert record.caller == "998901234567"
    assert record.turns, "nothing was recognised"
    assert record.turns[0].intent == "operator"
    assert "ok" in await http_get(control_port, "/health")
