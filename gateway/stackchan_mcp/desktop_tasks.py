# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""One line per desktop task the voice session hands off, and a spoken ending.

``send_task`` returns as soon as the desktop accepts the job. The voice turn
often ends before the result exists. This tracker keeps the request and, within
``DESKTOP_TASK_TIMEOUT_S``, either records the result or speaks a short failure
on the existing announce path.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# One place to change. A desktop lookup that has not landed by then is spoken as a failure.
DESKTOP_TASK_TIMEOUT_S = 90.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_desktop_task(
    task_id: str,
    started: str,
    outcome: str,
    *,
    ended: str | None = None,
    waited_ms: int = 0,
) -> None:
    """One line: id, start, end, outcome, how long a reconnect wait lasted. No request text."""
    logger.info(
        "desktop task id=%s start=%s end=%s outcome=%s waited_ms=%d",
        task_id,
        started,
        ended or _now(),
        outcome,
        int(waited_ms),
    )


def failure_phrase(request: str) -> str:
    text = " ".join(str(request or "").split())
    if len(text) > 80:
        text = text[:80].rstrip()
    if not text:
        text = "that"
    return f"I couldn't finish looking up {text}."


@dataclass
class _OpenTask:
    id: str
    request: str
    started: str
    deadline_mono: float
    waited_ms: int = 0
    timer: asyncio.Task | None = None
    done: bool = False


@dataclass
class DesktopTasks:
    """Tracks accepted desktop tasks for one voice bridge."""

    announce: object
    channel_ready: object
    timeout_s: float = DESKTOP_TASK_TIMEOUT_S
    _open: dict[str, _OpenTask] = field(default_factory=dict)
    _unsaid: list[dict] = field(default_factory=list)
    _cancelled: list = field(default_factory=list)

    def refuse(self, waited_ms: int = 0) -> None:
        """The desktop channel was down, so the tool did not hand the task off."""
        now = _now()
        log_desktop_task(
            uuid.uuid4().hex[:12], now, "channel_down", ended=now, waited_ms=waited_ms
        )

    def start(self, request: str, *, waited_ms: int = 0) -> str:
        task_id = uuid.uuid4().hex[:12]
        # The reconnect wait is part of the 90s budget, not extra time.
        budget = max(0.0, self.timeout_s - max(0, waited_ms) / 1000.0)
        task = _OpenTask(
            id=task_id,
            request=str(request or ""),
            started=_now(),
            deadline_mono=time.monotonic() + budget,
            waited_ms=max(0, int(waited_ms)),
        )
        self._open[task_id] = task
        task.timer = asyncio.create_task(self._expire(task), name="desktop-task-timeout")
        return task_id

    def landed(self, *, spoken: bool, text: str) -> None:
        """A desktop result arrived. ``spoken`` is false when the voice session could not take it."""
        task = self._oldest()
        if task is None:
            return
        self._finish_open(task)
        phrase = text.strip() if isinstance(text, str) else ""
        if spoken:
            log_desktop_task(task.id, task.started, "delivered", waited_ms=task.waited_ms)
            return
        self._unsaid.append(
            {
                "open": False,
                "id": task.id,
                "start": task.started,
                "outcome": "delivered",
                "waited_ms": task.waited_ms,
                "text": phrase or failure_phrase(task.request),
            }
        )

    def export_for_handoff(self) -> list[dict]:
        """Move unfinished work off this bridge so a new voice session can finish it."""
        now = time.monotonic()
        items: list[dict] = []
        for task in list(self._open.values()):
            self._finish_open(task)
            items.append(
                {
                    "open": True,
                    "id": task.id,
                    "request": task.request,
                    "start": task.started,
                    "remaining_s": max(0.0, task.deadline_mono - now),
                    "waited_ms": task.waited_ms,
                }
            )
        items.extend(self._unsaid)
        self._unsaid.clear()
        return items

    def adopt(self, item: dict) -> None:
        """Continue a task exported from the previous voice session."""
        if not item.get("open"):
            return
        task = _OpenTask(
            id=str(item["id"]),
            request=str(item.get("request") or ""),
            started=str(item["start"]),
            deadline_mono=time.monotonic() + float(item.get("remaining_s") or 0),
            waited_ms=int(item.get("waited_ms") or 0),
        )
        self._open[task.id] = task
        task.timer = asyncio.create_task(self._expire(task), name="desktop-task-timeout")

    def close(self) -> None:
        for task in list(self._open.values()):
            self._finish_open(task)
        self._open.clear()

    async def aclose(self) -> None:
        self.close()
        cancelled, self._cancelled = self._cancelled, []
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    def _oldest(self) -> _OpenTask | None:
        open_tasks = [task for task in self._open.values() if not task.done]
        if not open_tasks:
            return None
        return min(open_tasks, key=lambda task: task.deadline_mono)

    def _finish_open(self, task: _OpenTask) -> None:
        task.done = True
        timer = task.timer
        task.timer = None
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
            self._cancelled.append(timer)
        self._open.pop(task.id, None)

    async def _expire(self, task: _OpenTask) -> None:
        delay = task.deadline_mono - time.monotonic()
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
        if task.done or task.id not in self._open:
            return
        self._finish_open(task)
        ready = False
        try:
            ready = bool(self.channel_ready())
        except Exception:
            ready = False
        outcome = "timeout" if ready else "channel_down"
        phrase = failure_phrase(task.request)
        said = False
        try:
            said = bool(await self.announce(phrase))
        except Exception:
            logger.warning("desktop task announcement failed id=%s", task.id)
            said = False
        if said:
            log_desktop_task(task.id, task.started, outcome, waited_ms=task.waited_ms)
            return
        self._unsaid.append(
            {
                "open": False,
                "id": task.id,
                "start": task.started,
                "outcome": outcome,
                "waited_ms": task.waited_ms,
                "text": phrase,
            }
        )
