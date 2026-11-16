# SPDX-License-Identifier: MIT
"""One podcast turn: the guard, the ask seq, and the panel on screen.

The voice tools in the ``portal_podcast`` group share one guard. A context
``seq`` is the turn. With no context yet, a counter of device ``listen`` start
events is the turn. Logs carry the seq and a reason, never a title.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TurnGuard:
    """At most one successful open per turn, and at most one follow batch.

    Misses count. A fourth open attempt before any frame is ``capped``.
    After a frame goes out, later opens are ``one_open_per_turn``.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._key: tuple[Any, ...] | None = None
        self._calls = 0
        self._opened: str | None = None
        self._follow_used = False

    def claim_open(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        self._roll(key)
        self._calls += 1
        if self._opened:
            self._log(key, "one_open_per_turn")
            return {"ok": False, "reason": "one_open_per_turn", "opened": self._opened}
        if self._calls > 3:
            self._log(key, "capped")
            return {"ok": False, "reason": "capped"}
        return None

    def commit_open(self, title: str) -> None:
        self._opened = title

    def claim_follow(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        self._roll(key)
        if self._follow_used:
            self._log(key, "already_handled")
            return {"ok": False, "reason": "already_handled"}
        self._follow_used = True
        return None

    def _roll(self, key: tuple[Any, ...]) -> None:
        if key == self._key:
            return
        self._key = key
        self._calls = 0
        self._opened = None
        self._follow_used = False

    @staticmethod
    def _log(key: tuple[Any, ...], reason: str) -> None:
        label = key[1] if len(key) > 1 else key[0]
        logger.info("podcast guard seq=%s reason=%s", label, reason)


def shared_guard(esp32: Any) -> TurnGuard:
    guard = getattr(esp32, "podcast_guard", None)
    if isinstance(guard, TurnGuard):
        return guard
    guard = TurnGuard()
    try:
        setattr(esp32, "podcast_guard", guard)
    except Exception:
        return guard
    return guard


def turn_key(esp32: Any) -> tuple[Any, ...]:
    """Context seq when the Portal has sent one, otherwise the listen counter."""
    ctx = getattr(esp32, "koji_context", None)
    if isinstance(ctx, dict):
        seq = ctx.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            return ("seq", seq)
    starts = getattr(esp32, "listen_starts", 0)
    if not isinstance(starts, int) or isinstance(starts, bool):
        starts = 0
    return ("listen", starts)


def context_panel(esp32: Any) -> dict[str, Any] | None:
    ctx = getattr(esp32, "koji_context", None)
    if not isinstance(ctx, dict):
        return None
    panel = ctx.get("panel")
    if not isinstance(panel, dict):
        return None
    return panel
