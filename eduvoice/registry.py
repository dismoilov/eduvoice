"""Call state shared between the AudioSocket bridge and the dialplan (control API).

Asterisk cannot pass channel variables over AudioSocket, so the dialplan announces a call
here before it starts, and asks here what to do after the bridge hangs up.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

NextAction = Literal["operator", "hangup"]

# A call announced by the dialplan but never connected to the bridge is abandoned after
# this long: longer than any real call (MAX_CALL_S is 360 s), so a live one is never hit.
ABANDONED_AFTER_S = 900.0

# Safe default: if we know nothing about a call, a human takes it.
DEFAULT_NEXT: NextAction = "operator"


@dataclass(slots=True)
class Turn:
    """One exchange: what the caller asked and what the assistant did about it.

    `at_ms` is the moment the question started, counted from the beginning of the call —
    the CRM uses it to jump straight to that place in the recording.
    `source` names the document an FAQ answer came from, so an answer can be traced back
    to the regulation it is based on.
    """

    question: str
    answer: str
    intent: str
    latency_ms: dict[str, float] = field(default_factory=dict)
    action: str = ""
    faq_id: str = ""
    source: str = ""
    at_ms: float = 0.0


@dataclass(slots=True)
class CallRecord:
    call_id: str
    caller: str = ""
    started_at: float = field(default_factory=time.time)
    connected: bool = False
    next_action: NextAction = DEFAULT_NEXT
    turns: list[Turn] = field(default_factory=list)
    summary: str = ""
    ended_at: float | None = None
    # Where Asterisk's MixMonitor put the recording, relative to the recordings folder.
    recording: str = ""

    @property
    def duration_s(self) -> float:
        return (self.ended_at or time.time()) - self.started_at


class CallRegistry:
    def __init__(self, keep_last: int = 200) -> None:
        self._calls: dict[str, CallRecord] = {}
        self._order: list[str] = []
        self._keep_last = keep_last

    def start(self, call_id: str, caller: str = "") -> CallRecord:
        """Announces a call. Repeating it (a dialplan retry) keeps the existing record."""
        existing = self._calls.get(call_id)
        if existing is not None:
            if caller and not existing.caller:
                existing.caller = caller
            return existing
        record = CallRecord(call_id=call_id, caller=caller)
        self._calls[call_id] = record
        self._order.append(call_id)
        self._forget_old()
        return record

    def get(self, call_id: str) -> CallRecord | None:
        return self._calls.get(call_id)

    def ensure(self, call_id: str) -> CallRecord:
        """A call may reach the bridge without /start (e.g. a test originate)."""
        return self._calls.get(call_id) or self.start(call_id)

    def set_next(self, call_id: str, action: NextAction) -> None:
        record = self.ensure(call_id)
        record.next_action = action

    def next_action(self, call_id: str) -> NextAction:
        record = self._calls.get(call_id)
        return record.next_action if record else DEFAULT_NEXT

    def finish(self, call_id: str, summary: str = "") -> None:
        record = self._calls.get(call_id)
        if record is None:
            return
        record.ended_at = time.time()
        if summary:
            record.summary = summary

    @property
    def active(self) -> int:
        return sum(1 for r in self._calls.values() if r.ended_at is None and r.connected)

    def _forget_old(self) -> None:
        """Keeps memory bounded. A call that is still running is never forgotten — losing
        it would make the dialplan ask about an unknown call later.

        "Still running" has to mean more than "never finished". The dialplan announces a
        call before it dials, so a caller who rings off in that second, or a dial that
        fails, leaves a record that no session will ever finish. Pinned for ever, those
        accumulate: measured, forty thousand of them made one new announcement block the
        event loop for 126 ms, which every caller on the line hears as the same gap at the
        same instant. A record that never reached the bridge is therefore forgotten once
        it is older than a whole call could be.
        """
        if len(self._order) <= self._keep_last:
            return
        cutoff = time.time() - ABANDONED_AFTER_S
        keep: list[str] = []
        excess = len(self._order) - self._keep_last
        for position, call_id in enumerate(self._order):
            record = self._calls.get(call_id)
            if record is None:
                continue
            running = record.ended_at is None and (record.connected or record.started_at > cutoff)
            if position < excess and not running:
                self._calls.pop(call_id, None)
                continue
            keep.append(call_id)
        self._order = keep


registry = CallRegistry()
