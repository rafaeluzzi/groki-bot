# SPDX-License-Identifier: MIT
"""Silent conversation items. They never ask Grok to reply."""

from __future__ import annotations

import json
from typing import Any

QUIET_PREFIX = "[portal status, context only, do not reply]"
RATE_S = 2.0
MAX_EVENTS = 5
TURN_PENDING_S = 45.0


class QuietContext:
    """Coalesce, defer while a response is active, and rate-limit to one item per 2 s."""

    def __init__(self) -> None:
        self.snapshot: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self._last_sent = -1_000.0
        self._pending_system = False
        self.system_rejected = False
        self.turn_pending = False
        self.turn_pending_at: float | None = None
        self.attached: list[dict[str, Any]] = []
        self._ids = 0
        self._sent_events: list[dict[str, Any]] = []
        self._sent_snapshot: dict[str, Any] | None = None

    def offer(self, snapshot: dict[str, Any] | None, events: list[dict[str, Any]]) -> None:
        if isinstance(snapshot, dict):
            self.snapshot = snapshot
        for event in events:
            if isinstance(event, dict):
                self.events.append(event)
        if len(self.events) > MAX_EVENTS:
            self.events = self.events[-MAX_EVENTS:]

    def hold_turn(self, now: float) -> None:
        self.turn_pending = True
        if self.turn_pending_at is None:
            self.turn_pending_at = now

    def release_turn(self) -> None:
        self.turn_pending = False
        self.turn_pending_at = None

    def expire_turn(self, now: float, limit: float = TURN_PENDING_S) -> bool:
        """Drop a turn that never reached a reply. A new hold does not extend the clock."""
        started = self.turn_pending_at
        if not self.turn_pending or started is None or now - started < limit:
            return False
        self.release_turn()
        return True

    def due(self, now: float, *, response_active: bool, item_id: str | None = None) -> dict[str, Any] | None:
        """The next item to send, or None when it must wait.

        A snapshot alone is not an item. Events carry the snapshot with them.
        """
        self.expire_turn(now)
        if not self.events:
            return None
        if response_active or self.turn_pending:
            return None
        if now - self._last_sent < RATE_S:
            return None
        self._last_sent = now
        self._pending_system = not self.system_rejected
        self._sent_events = list(self.events[-MAX_EVENTS:])
        self._sent_snapshot = self.snapshot
        return self._item(system=not self.system_rejected, item_id=item_id or self._next_id())

    def in_flight(self) -> list[dict[str, Any]]:
        """Event objects included in the item due() last built."""
        return list(self._sent_events)

    def note_sent(self) -> None:
        """Drop only the batch that was sent. A newer offer stays queued."""
        sent_snapshot = self._sent_snapshot
        self._drop(self._sent_events)
        if self.snapshot is sent_snapshot:
            self.snapshot = None
        self._pending_system = False
        self._sent_events = []
        self._sent_snapshot = None

    def note_rejected(self, item_id: str | None = None) -> dict[str, Any] | None:
        """The server refused a system item. Retry once as a user item, else keep the events."""
        if not self._pending_system:
            self.attached = self._owed()
            return None
        self.system_rejected = True
        self._pending_system = False
        if not self._owed():
            return None
        return self._item(system=False, item_id=item_id or self._next_id())

    def _next_id(self) -> str:
        self._ids += 1
        return f"quiet_{self._ids}"

    def fail(self) -> None:
        """Injection is unsupported. The next tool result carries the events."""
        self.attached = self._owed()
        self._pending_system = False

    def take_attached(self) -> list[dict[str, Any]]:
        held = self.attached
        self.attached = []
        self._drop(held)
        if self._sent_events and all(id(event) in {id(item) for item in held} for event in self._sent_events):
            self._sent_events = []
        return held

    def _owed(self) -> list[dict[str, Any]]:
        """The in-flight item when one was sent. Otherwise everything still queued."""
        if self._sent_events:
            return list(self._sent_events)
        return list(self.events)

    def _drop(self, events: list[dict[str, Any]]) -> None:
        gone = {id(event) for event in events}
        self.events = [event for event in self.events if id(event) not in gone]

    def _item(self, *, system: bool, item_id: str) -> dict[str, Any]:
        line = _line(self._sent_snapshot, self._sent_events)
        text = line if system else f"{QUIET_PREFIX} {line}"
        role = "system" if system else "user"
        return {
            "type": "conversation.item.create",
            "item": {
                "id": item_id,
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": text}],
            },
        }


def _line(snapshot: dict[str, Any] | None, events: list[dict[str, Any]]) -> str:
    body = {
        "portal_state": _compact(snapshot) if isinstance(snapshot, dict) else None,
        "portal_events": [_compact(event) for event in events[-MAX_EVENTS:]],
    }
    return "Portal status updated. " + json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown and title-like keys from an event. Status titles stay; they are for Grok."""
    return dict(value)
