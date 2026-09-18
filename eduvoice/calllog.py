"""One JSON line per finished call: what was asked, what was decided, how long it took.

The CRM keeps the same data in the database; this file stays as the backup that survives
a database problem, and as the simplest thing to read over ssh when something is wrong.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from eduvoice.registry import CallRecord

log = logging.getLogger("eduvoice.calllog")


def call_as_dict(record: CallRecord, ended_reason: str = "") -> dict[str, Any]:
    """The shape shared by the JSON log and the database."""
    return {
        "call_id": record.call_id,
        "caller": record.caller,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.started_at)),
        "duration_s": round(record.duration_s, 1),
        "next_action": record.next_action,
        "ended_reason": ended_reason,
        "recording": record.recording,
        "turns": [asdict(turn) for turn in record.turns],
        "summary": record.summary,
    }


class CallLog:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def write(self, record: CallRecord, ended_reason: str = "") -> None:
        self.write_entry(call_as_dict(record, ended_reason))

    def write_entry(self, entry: dict[str, Any]) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            path = self._directory / f"{time.strftime('%Y%m%d')}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:  # logging must never break a call
            log.warning("could not write the call log: %s", exc)
