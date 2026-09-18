import asyncio
import json
import uuid

import pytest

from eduvoice.control_api import serve_control_api
from eduvoice.registry import CallRegistry


@pytest.fixture
async def api():
    registry = CallRegistry()
    server = await serve_control_api(registry, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield registry, port
    server.close()
    await server.wait_closed()


async def request(port: int, method: str, path: str, body: str = "") -> tuple[str, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if body:
        head += (
            f"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: {len(body)}\r\n"
        )
    writer.write((head + "\r\n" + body).encode())
    await writer.drain()
    raw = (await reader.read()).decode()
    writer.close()
    status_line, _, rest = raw.partition("\r\n")
    _, payload = rest.split("\r\n\r\n", 1)
    return status_line.split(" ", 1)[1], payload


async def test_dialplan_announces_a_call_and_gets_ok(api):
    registry, port = api
    call_id = str(uuid.uuid4())

    status, body = await request(port, "POST", f"/calls/{call_id}/start", "caller=998901234567")

    assert status.startswith("200")
    assert body == "ok"
    record = registry.get(call_id)
    assert record is not None and record.caller == "998901234567"


async def test_next_defaults_to_operator_for_a_known_call(api):
    registry, port = api
    call_id = str(uuid.uuid4())
    registry.start(call_id)

    status, body = await request(port, "GET", f"/calls/{call_id}/next")

    assert status.startswith("200")
    assert body == "operator"


async def test_next_returns_the_decision_made_by_the_bridge(api):
    registry, port = api
    call_id = str(uuid.uuid4())
    registry.start(call_id)
    registry.set_next(call_id, "hangup")

    _, body = await request(port, "GET", f"/calls/{call_id}/next")

    assert body == "hangup"


async def test_unknown_call_is_sent_to_an_operator(api):
    _registry, port = api

    _, body = await request(port, "GET", f"/calls/{uuid.uuid4()}/next")

    assert body == "operator", "a caller must never be dropped into silence"


async def test_summary_returns_call_data(api):
    registry, port = api
    call_id = str(uuid.uuid4())
    registry.start(call_id, caller="998901234567")
    registry.finish(call_id, summary="stipendiya haqida savol")

    status, body = await request(port, "GET", f"/calls/{call_id}/summary")

    assert status.startswith("200")
    data = json.loads(body)
    assert data["caller"] == "998901234567"
    assert data["summary"] == "stipendiya haqida savol"


async def test_health_reports_status(api):
    _registry, port = api

    status, body = await request(port, "GET", "/health")

    assert status.startswith("200")
    assert json.loads(body)["status"] == "ok"


async def test_unknown_path_is_404(api):
    _registry, port = api

    status, _ = await request(port, "GET", "/nope")

    assert status.startswith("404")


async def test_a_malformed_request_still_gets_an_answer(api):
    """Asterisk's CURL() must never wait: a broken request is answered with the safe default."""
    _registry, port = api
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /calls/x/next HTTP/1.1\r\nContent-Length: not-a-number\r\n\r\n")
    await writer.drain()

    raw = await asyncio.wait_for(reader.read(), timeout=3)
    writer.close()

    assert raw, "the dialplan got no answer at all"
    assert raw.decode().rstrip().endswith("operator")


async def test_a_repeated_start_keeps_what_the_bridge_already_recorded(api):
    """A dialplan retry must not wipe the turns of a call that is already running."""
    registry, port = api
    call_id = str(uuid.uuid4())
    await request(port, "POST", f"/calls/{call_id}/start", "caller=998901234567")
    registry.set_next(call_id, "hangup")

    await request(port, "POST", f"/calls/{call_id}/start", "caller=998901234567")

    assert registry.next_action(call_id) == "hangup"


async def test_a_flood_of_announcements_cannot_evict_a_running_call(api):
    """Memory stays bounded, but a live call is never forgotten."""
    registry, port = api
    live = str(uuid.uuid4())
    await request(port, "POST", f"/calls/{live}/start", "caller=998900000000")
    registry.get(live).connected = True

    for _ in range(250):
        other = str(uuid.uuid4())
        registry.start(other)
        registry.finish(other)

    assert registry.get(live) is not None
    assert registry.next_action(live) == "operator"
