"""Serving call recordings.

Carried over from the old panel because it was the one piece already proven in front of
a browser: WAV with byte ranges (so the player can seek) and a name that can never point
outside the recordings folder.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from crm import repo
from crm.config import settings
from crm.deps import current_user, get_db

router = APIRouter(prefix="/media")

CALL_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CHUNK = 64 * 1024


def recording_path(db: sqlite3.Connection, call_id: str) -> Path | None:
    """Finds the WAV of a call; refuses anything that is not a plain call id."""
    call_id = call_id.removesuffix(".wav")
    if not CALL_ID.match(call_id):
        return None
    row = repo.call_by_uuid(db, call_id)
    candidates = []
    if row and row["recording"]:
        candidates.append(settings.recordings_dir / row["recording"])
    # Any date folder, under the current name or the one used before the rename.
    candidates += sorted(settings.recordings_dir.glob(f"*/*/{call_id}.wav"), reverse=True)
    base = settings.recordings_dir.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and base in resolved.parents:
            return resolved
    return None


def parse_range(header: str, size: int) -> tuple[int, int]:
    """`Range: bytes=start-end` -> the byte range to send. Anything odd means all of it."""
    if not header.startswith("bytes=") or "," in header:
        return 0, max(0, size - 1)
    raw_start, _, raw_end = header.removeprefix("bytes=").partition("-")
    try:
        if not raw_start:
            length = min(int(raw_end), size)
            return size - length, size - 1
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
    except ValueError:
        return 0, max(0, size - 1)
    start = max(0, min(start, size - 1))
    return start, max(start, min(end, size - 1))


@router.get("/recordings/{call_id}")
def recording(
    call_id: str,
    request: Request,
    _user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    path = recording_path(db, call_id)
    if path is None:
        raise HTTPException(404, "no recording")

    size = path.stat().st_size
    if size == 0:  # a recording that never got written must not hang the player
        raise HTTPException(404, "empty recording")
    start, end = parse_range(request.headers.get("range", ""), size)
    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "no-store",
    }
    status_code = 200
    if not (start == 0 and length == size):
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        status_code = 206

    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers, media_type="audio/wav")

    def stream():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                data = handle.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    # "audio/wav" on purpose: some browsers refuse the "audio/x-wav" that mimetypes guesses.
    return StreamingResponse(
        stream(), status_code=status_code, headers=headers, media_type="audio/wav"
    )
