# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""One line per desktop task the voice session hands off, and a spoken ending.

``send_task`` returns as soon as the desktop accepts the job. The voice turn
often ends before the result exists. This tracker keeps the request until the
timeout, or until the box has gone quiet after a real reply.

Box replies, including holding lines, are spoken as their own updates. A
holding line does not finish the task. That matches Grok Bot's own voice
handler, which speaks every text ``send-message`` on ``voice:<callId>``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# One place to change. A desktop lookup that has not landed by then is spoken as a failure.
DESKTOP_TASK_TIMEOUT_S = 90.0
# No transcript reply by then: one silent resend of the same request. Never a second.
DESKTOP_TASK_RETRY_S = 45.0
# Quiet after a non-holding reply has finished playing, before the task is finished.
# It does not start when the reply is queued: 13e84dcebc96 closed while Koji was
# still speaking the answer, and that answer never logged spoken.
# Holding lines do not start this clock. 12s is long enough for a second bubble
# of the same answer and well under the 90s failure clock. Anything later is
# spoken once as a late result.
DESKTOP_TASK_QUIET_S = 12.0
# A reply stamped just before we logged the task start still belongs to it.
# Transcript request stamps land within ~40 ms of our start log, and a reply takes
# seconds. 3s credited the Cary list (21:52:30.7) to Durham, sent at 21:52:32.1.
REPLY_SKEW_MS = 1000
# The channel was up, and no reply had arrived.
TIMEOUT_UP_PHRASE = "That one didn't come back in time."
# The channel was down when the wait ended.
TIMEOUT_DOWN_PHRASE = "Desktop tasks are down right now."

# Progress beats the box sends before the answer. Spoken, but they leave the task open.
# The transcript uses a straight apostrophe ("I'm looking up … now."). The pattern
# used to require U+2019, so that line started the quiet clock.
# The progress verbs are looking, checking, pulling, and searching.
# "I'm seeing three shows open now." is an answer, not a holding line.
_APOSTROPHE = str.maketrans({"\u2018": "'", "\u2019": "'", "`": "'"})
_HOLDING_RE = re.compile(
    r"^(looking up|still looking|still searching|searching|checking|working on|"
    r"let me look|i'll look|i'm looking|i'm checking|on it|one moment)\b",
    re.IGNORECASE,
)
_HOLDING_NOW_RE = re.compile(
    r"^i'm (looking|checking|pulling|searching)\b.*\bnow\.?$",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


def is_holding_line(text: str) -> bool:
    """True for a progress beat the box sends before the answer.

    Seen on the voice transcript: "I'm looking up San Juan events for November
    14 and 15 now.", "I'm checking Saturday's forecast for Durham now.",
    "I'm pulling Saturday's hourly forecast for Durham now.", "Looking up…",
    "Still searching". "I'm seeing three shows open now." is an answer.
    A holding line is spoken and does not start the quiet clock.
    """
    compact = " ".join(str(text or "").split()).translate(_APOSTROPHE)
    if not compact or len(compact) > 200:
        return False
    if _HOLDING_RE.match(compact) or _HOLDING_NOW_RE.match(compact):
        return True
    return len(compact) <= 120 and (compact.endswith("…") or compact.endswith("..."))


def late_phrase(request: str, text: str) -> str:
    """A result that showed up after its task had already finished."""
    label = " ".join(str(request or "").split())
    if len(label) > 60:
        label = label[:60].rstrip()
    if not label:
        label = "lookup"
    body = " ".join(str(text or "").split())
    return f"That {label} just came back: {body}"


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


def handoff_already_closed(item: dict) -> bool:
    """True when this handoff item already has a terminal outcome.

    Quiet, timeout, and delivered tasks are exported so a new session can still
    name a late reply. Closing the voice session must not log ``failed`` again.
    """
    return bool(item.get("remember") or item.get("outcome_logged"))


def log_desktop_hop(task_id: str, hop: str, **fields: object) -> None:
    """One hop for a task. Extra fields are fixed words, never the request or the result."""
    extra = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("desktop task id=%s hop=%s%s", task_id, hop, f" {extra}" if extra else "")


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
    started_ms: int
    deadline_mono: float
    waited_ms: int = 0
    replies: int = 0
    resent: bool = False
    timer: asyncio.Task | None = None
    retry: asyncio.Task | None = None
    quiet: asyncio.Task | None = None
    quiet_deadline: float = 0.0
    pending_playback: int = 0
    done: bool = False


@dataclass
class _Finished:
    id: str
    request: str
    started: str
    started_ms: int
    waited_ms: int


@dataclass
class DesktopTasks:
    """Tracks accepted desktop tasks for one voice bridge."""

    announce: object
    channel_ready: object
    resend: object = None
    timeout_s: float = DESKTOP_TASK_TIMEOUT_S
    quiet_s: float = DESKTOP_TASK_QUIET_S
    retry_s: float = DESKTOP_TASK_RETRY_S
    _open: dict[str, _OpenTask] = field(default_factory=dict)
    _finished: list[_Finished] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)
    _outcome_ids: set[str] = field(default_factory=set)
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
            started_ms=_now_ms(),
            deadline_mono=time.monotonic() + budget,
            waited_ms=max(0, int(waited_ms)),
        )
        self._open[task_id] = task
        task.timer = asyncio.create_task(self._expire(task), name="desktop-task-timeout")
        task.retry = asyncio.create_task(self._retry(task), name="desktop-task-retry")
        log_desktop_hop(task_id, "accepted", at=task.started)
        return task_id

    def accept_reply(self, entry_id: str, text: str, at_ms: int) -> dict | None:
        """Attach one transcript reply to a task. Does not log delivery.

        Returns the words to speak, or None when this entry was already spoken
        or is older than every task we still owe.
        """
        phrase = text.strip() if isinstance(text, str) else ""
        if not phrase:
            return None
        key = str(entry_id or "")
        if not key or key in self._seen:
            return None
        at = int(at_ms) if isinstance(at_ms, (int, float)) else _now_ms()
        oldest = self._oldest_started()
        if oldest is not None and at + REPLY_SKEW_MS < oldest.started_ms:
            return None
        task = self._owner(at)
        if task is not None:
            self._seen.add(key)
            task.replies += 1
            self._cancel_retry(task)
            holding = is_holding_line(phrase)
            # The quiet clock starts when this reply finishes playing, not now.
            task.pending_playback += 1
            self._cancel_quiet(task)
            log_desktop_hop(task.id, "queued", holding=int(holding), late=0)
            return self._decision(task, phrase, late=False)
        owner = self._late_owner(at)
        if owner is None:
            return None
        self._seen.add(key)
        log_desktop_hop(owner.id, "queued", holding=0, late=1)
        return {
            "task_id": owner.id,
            "started": owner.started,
            "waited_ms": owner.waited_ms,
            "say": late_phrase(owner.request, phrase),
            "late": True,
        }

    def stash(self, decision: dict) -> None:
        """The voice session could not take this reply. The next one speaks it."""
        self._unsaid.append(
            {
                "open": False,
                "reply": True,
                "id": decision["task_id"],
                "start": decision["started"],
                "waited_ms": decision["waited_ms"],
                "text": decision["say"],
            }
        )

    def remember(self, item: dict) -> None:
        """Keep a finished task so a late reply can still name it."""
        if any(task.id == str(item.get("id")) for task in self._finished):
            return
        self._finished.append(
            _Finished(
                id=str(item.get("id") or ""),
                request=str(item.get("request") or ""),
                started=str(item.get("start") or _now()),
                started_ms=int(item.get("started_ms") or 0),
                waited_ms=int(item.get("waited_ms") or 0),
            )
        )
        for entry_id in item.get("seen") or []:
            if isinstance(entry_id, str) and entry_id:
                self._seen.add(entry_id)

    def export_for_handoff(self) -> list[dict]:
        """Move unfinished work off this bridge so a new voice session can finish it."""
        now = time.monotonic()
        seen = list(self._seen)
        finished = list(self._finished)
        self._finished.clear()
        items: list[dict] = []
        for task in list(self._open.values()):
            quiet_left = None
            if task.quiet is not None and not task.quiet.done():
                # The quiet clock is wall time from when it was armed; export the remainder.
                quiet_left = max(0.0, task.quiet_deadline - now)
            self._finish_open(task, remember=False)
            items.append(
                {
                    "open": True,
                    "id": task.id,
                    "request": task.request,
                    "start": task.started,
                    "started_ms": task.started_ms,
                    "remaining_s": max(0.0, task.deadline_mono - now),
                    "waited_ms": task.waited_ms,
                    "replies": task.replies,
                    "resent": task.resent,
                    "quiet_remaining_s": quiet_left,
                    "pending_playback": task.pending_playback,
                    "outcome_logged": task.id in self._outcome_ids,
                    "seen": seen,
                }
            )
        items.extend(self._unsaid)
        self._unsaid.clear()
        for done in finished:
            items.append(
                {
                    "open": False,
                    "remember": True,
                    "id": done.id,
                    "request": done.request,
                    "start": done.started,
                    "started_ms": done.started_ms,
                "waited_ms": done.waited_ms,
                "outcome_logged": True,
                "seen": seen,
            }
            )
        self._seen.clear()
        return items

    def adopt(self, item: dict) -> None:
        """Continue a task exported from the previous voice session."""
        for entry_id in item.get("seen") or []:
            if isinstance(entry_id, str) and entry_id:
                self._seen.add(entry_id)
        if not item.get("open"):
            return
        task = _OpenTask(
            id=str(item["id"]),
            request=str(item.get("request") or ""),
            started=str(item["start"]),
            started_ms=int(item.get("started_ms") or _now_ms()),
            deadline_mono=time.monotonic() + float(item.get("remaining_s") or 0),
            waited_ms=int(item.get("waited_ms") or 0),
            replies=int(item.get("replies") or 0),
            resent=bool(item.get("resent")),
            pending_playback=int(item.get("pending_playback") or 0),
        )
        if item.get("outcome_logged"):
            self._outcome_ids.add(task.id)
        self._open[task.id] = task
        task.timer = asyncio.create_task(self._expire(task), name="desktop-task-timeout")
        if not task.resent and not task.replies:
            task.retry = asyncio.create_task(self._retry(task), name="desktop-task-retry")
        quiet_left = item.get("quiet_remaining_s")
        if (
            isinstance(quiet_left, (int, float))
            and task.replies
            and task.pending_playback == 0
        ):
            self._arm_quiet(task, delay=max(0.0, float(quiet_left)))

    def note_outcome(self, task_id: str) -> None:
        """This task already has a spoken, cancelled, or timeout outcome line."""
        if task_id:
            self._outcome_ids.add(str(task_id))

    def playback_finished(self, task_id: str, *, holding: bool) -> None:
        """One queued reply finished playing. A real answer then starts the quiet clock.

        A second reply still waiting to play does not. A holding line never does.
        """
        task = self._open.get(str(task_id or ""))
        if task is None or task.done:
            return
        if task.pending_playback > 0:
            task.pending_playback -= 1
        if holding or task.pending_playback > 0:
            self._cancel_quiet(task)
            return
        self._arm_quiet(task)

    def oldest(self) -> _OpenTask | None:
        return self._oldest()

    def close(self) -> None:
        for task in list(self._open.values()):
            self._finish_open(task, remember=False)
        self._open.clear()

    async def aclose(self) -> None:
        self.close()
        cancelled, self._cancelled = self._cancelled, []
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    def _decision(self, task: _OpenTask, phrase: str, *, late: bool) -> dict:
        return {
            "task_id": task.id,
            "started": task.started,
            "waited_ms": task.waited_ms,
            "say": phrase,
            "late": late,
        }

    def _owner(self, at_ms: int) -> _OpenTask | None:
        open_tasks = [task for task in self._open.values() if not task.done]
        if not open_tasks:
            return None
        oldest = min(open_tasks, key=lambda task: task.started_ms)
        if at_ms + REPLY_SKEW_MS < oldest.started_ms:
            return None
        candidates = [task for task in open_tasks if task.started_ms <= at_ms + REPLY_SKEW_MS]
        if not candidates:
            return oldest
        return max(candidates, key=lambda task: task.started_ms)

    def _late_owner(self, at_ms: int) -> _Finished | None:
        candidates = [
            task for task in self._finished if task.started_ms <= at_ms + REPLY_SKEW_MS
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda task: task.started_ms)

    def _oldest_started(self) -> _OpenTask | None:
        open_tasks = [task for task in self._open.values() if not task.done]
        if not open_tasks:
            return None
        return min(open_tasks, key=lambda task: task.started_ms)

    def _oldest(self) -> _OpenTask | None:
        open_tasks = [task for task in self._open.values() if not task.done]
        if not open_tasks:
            return None
        return min(open_tasks, key=lambda task: task.deadline_mono)

    def _cancel_retry(self, task: _OpenTask) -> None:
        timer = task.retry
        task.retry = None
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
            self._cancelled.append(timer)

    def _cancel_quiet(self, task: _OpenTask) -> None:
        timer = task.quiet
        task.quiet = None
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
            self._cancelled.append(timer)

    def _arm_quiet(self, task: _OpenTask, delay: float | None = None) -> None:
        self._cancel_quiet(task)
        wait = self.quiet_s if delay is None else delay
        task.quiet_deadline = time.monotonic() + wait
        task.quiet = asyncio.create_task(self._quiet(task, wait), name="desktop-task-quiet")

    def _finish_open(self, task: _OpenTask, *, remember: bool) -> None:
        if task.done:
            return
        task.done = True
        self._cancel_quiet(task)
        self._cancel_retry(task)
        timer = task.timer
        task.timer = None
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
            self._cancelled.append(timer)
        self._open.pop(task.id, None)
        if remember:
            self._finished.append(
                _Finished(
                    id=task.id,
                    request=task.request,
                    started=task.started,
                    started_ms=task.started_ms,
                    waited_ms=task.waited_ms,
                )
            )
            if len(self._finished) > 20:
                del self._finished[:-20]

    async def _quiet(self, task: _OpenTask, delay: float) -> None:
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
        if task.done or task.id not in self._open:
            return
        self._finish_open(task, remember=True)
        log_desktop_hop(task.id, "quiet")

    async def _retry(self, task: _OpenTask) -> None:
        """One silent resend if the box never wrote a reply. Never a second."""
        delay = self.retry_s
        remaining = task.deadline_mono - time.monotonic()
        # Skip when the timeout would fire at or before this retry.
        if delay <= 0 or remaining <= delay or self.resend is None:
            return
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if task.done or task.id not in self._open or task.replies or task.resent:
            return
        task.resent = True
        log_desktop_hop(task.id, "resend")
        try:
            await self.resend(task.request)
        except Exception:
            logger.warning("desktop task resend failed id=%s", task.id)

    async def _expire(self, task: _OpenTask) -> None:
        delay = task.deadline_mono - time.monotonic()
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
        if task.done or task.id not in self._open:
            return
        # A holding line or a real answer already arrived. Don't also say it failed.
        if task.replies:
            self._finish_open(task, remember=True)
            log_desktop_hop(task.id, "timeout_skipped")
            return
        self._finish_open(task, remember=True)
        ready = False
        try:
            ready = bool(self.channel_ready())
        except Exception:
            ready = False
        outcome = "timeout" if ready else "channel_down"
        phrase = TIMEOUT_UP_PHRASE if ready else TIMEOUT_DOWN_PHRASE
        said = False
        try:
            said = bool(await self.announce(phrase))
        except Exception:
            logger.warning("desktop task announcement failed id=%s", task.id)
            said = False
        if said:
            self.note_outcome(task.id)
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
