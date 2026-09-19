"""Tiny HTTP API the dialplan talks to through Asterisk's CURL() function.

Kept dependency-free on purpose: the server runs on Sangoma Linux 7 where new wheels
often do not build, and the whole surface is three endpoints.

    POST /calls/{uuid}/start   caller=998901234567&format=ulaw  -> "ok"
    GET  /calls/{uuid}/next                            -> "operator" | "hangup"
    GET  /calls/{uuid}/summary                         -> JSON
    GET  /health                                       -> JSON

Unknown call id always answers "operator": a caller must never be left in silence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from urllib.parse import parse_qs, unquote

from eduvoice.registry import DEFAULT_NEXT, CallRegistry

log = logging.getLogger("eduvoice.control")

MAX_REQUEST_BYTES = 64 * 1024


def _response(status: str, body: str, content_type: str = "text/plain; charset=utf-8") -> bytes:
    payload = body.encode()
    head = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + payload


class ControlApi:
    def __init__(self, registry: CallRegistry, health: dict | None = None) -> None:
        self._registry = registry
        self._health = health if health is not None else {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not request_line:
                return
            method, raw_path, *_ = request_line.decode("latin-1").split()
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            length = min(int(headers.get("content-length", 0)), MAX_REQUEST_BYTES)
            body = (await reader.readexactly(length)).decode() if length else ""
            response = self._route(method, unquote(raw_path), body)
        except (TimeoutError, asyncio.IncompleteReadError, ValueError, UnicodeDecodeError) as exc:
            # Always answer something: Asterisk's CURL() would otherwise wait for its
            # own timeout and the caller would hear silence for those seconds.
            log.warning("bad control request: %s", exc)
            response = _response("400 Bad Request", DEFAULT_NEXT)
        try:
            writer.write(response)
            await writer.drain()
        except OSError as exc:
            log.warning("control client went away: %s", exc)
        finally:
            writer.close()

    def _route(self, method: str, path: str, body: str) -> bytes:
        path = path.split("?")[0].rstrip("/")
        parts = [p for p in path.split("/") if p]

        if method == "GET" and parts == ["health"]:
            payload = {"status": "ok", "active_calls": self._registry.active, **self._health}
            return _response("200 OK", json.dumps(payload), "application/json")

        if len(parts) == 3 and parts[0] == "calls":
            call_id, action = parts[1], parts[2]

            if method == "POST" and action == "start":
                form = parse_qs(body)
                caller = form.get("caller", [""])[0]
                started = self._registry.start(call_id, caller=caller)
                started.audio_format = form.get("format", [""])[0].strip().lower()
                log.info(
                    "call %s announced by dialplan (caller=%s, format=%s)",
                    call_id,
                    caller or "unknown",
                    started.audio_format or "unknown",
                )
                return _response("200 OK", "ok")

            if method == "GET" and action == "next":
                action_for_dialplan = self._registry.next_action(call_id)
                if self._registry.get(call_id) is None:
                    log.warning(
                        "unknown call %s asked for next action -> %s", call_id, DEFAULT_NEXT
                    )
                return _response("200 OK", action_for_dialplan)

            if method == "GET" and action == "summary":
                record = self._registry.get(call_id)
                if record is None:
                    return _response("404 Not Found", "{}", "application/json")
                return _response(
                    "200 OK", json.dumps(asdict(record), default=str), "application/json"
                )

        return _response("404 Not Found", "not found")


async def serve_control_api(
    registry: CallRegistry, host: str, port: int, health: dict | None = None
) -> asyncio.Server:
    api = ControlApi(registry, health)
    return await asyncio.start_server(api.handle, host, port)
