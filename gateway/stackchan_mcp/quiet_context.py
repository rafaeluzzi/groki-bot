# SPDX-License-Identifier: MIT
"""Silent conversation items. They never ask Grok to reply."""

from __future__ import annotations

import json
from typing import Any

QUIET_PREFIX = "[portal status, context only, do not reply]"
RATE_S = 2.0
MAX_EVENTS = 5


class QuietContext:
    """Coalesce, defer while a response is active, and rate-limit to one item per 2 s."""

    def __init__(self) -> None:
        self.snapshot: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self._last_sent = -1_000.0
        self._pending_system = False
        self.system_rejected = False
        self.attached: list[dict[str, Any]] = []

    def offer(self, snapshot: dict[str, Any] | None, events: list[dict[str, Any]]) -> None:
        if isinstance(snapshot, dict):
            self.snapshot = snapshot
        for event in events:
            if isinstance(event, dict):
                self.events.append(event)
        if len(self.events) > MAX_EVENTS:
            self.events = self.events[-MAX_EVENTS:]

    def due(self, now: float, *, response_active: bool) -> dict[str, Any] | None:
        """The next item to send, or None when it must wait."""
        if self.snapshot is None and not self.events:
            return None
        if response_active:
            return None
        if now - self._last_sent < RATE_S:
            return None
        self._last_sent = now
        self._pending_system = not self.system_rejected
        return self._item(system=not self.system_rejected)

    def note_sent(self) -> None:
        self.snapshot = None
        self.events = []
        self._pending_system = False

    def note_rejected(self) -> dict[str, Any] | None:
        """The server refused a system item. Retry once as a user item, else keep the events."""
        if not self._pending_system:
            self.attached = list(self.events)
            return None
        self.system_rejected = True
        self._pending_system = False
        if self.snapshot is None and not self.events:
            return None
        return self._item(system=False)

    def fail(self) -> None:
        """Injection is unsupported. The next tool result carries the events."""
        self.attached = list(self.events)
        self._pending_system = False

    def take_attached(self) -> list[dict[str, Any]]:
        held = self.attached
        self.attached = []
        self.events = []
        return held

    def _item(self, *, system: bool) -> dict[str, Any]:
        line = _line(self.snapshot, self.events)
        text = line if system else f"{QUIET_PREFIX} {line}"
        role = "system" if system else "user"
        return {
            "type": "conversation.item.create",
            "item": {
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
