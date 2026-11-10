# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Filtered local-helper channel for native Grok Bot presence and voice updates."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import weakref
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)

# A send_task during a short reconnect waits this long for the box stream to return.
RECONNECT_GRACE_S = 8.0
# Ready more recently than this is a blip (the pod drops the socket and presence
# returns within a few seconds). Down longer than this fails at once.
RECONNECT_RECENT_S = 30.0
# Skip another desktop wake when one succeeded, or a task was sent, more recently than this.
WARM_SKIP_S = 300.0

# The box stream's nudge is only a "read the transcript" signal. An ack is not.
NUDGE_SIGNAL_KINDS = frozenset({"progress", "outcome"})

# The helper already reduces its errors to fixed words such as upstream_http_429.
NATIVE_ERROR_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

_open_channels = weakref.WeakSet()


async def close_native_channels(timeout=5.0):
    """Hang up every started channel. Grok answers 417 to the next call while one is left on the line."""
    channels = list(_open_channels)
    if not channels:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*(c.close() for c in channels), return_exceptions=True), timeout)
    except asyncio.TimeoutError:
        logger.warning("Grok native hangup on shutdown timed out channels=%d", len(channels))


def _safe_word(value):
    if isinstance(value, str) and NATIVE_ERROR_RE.fullmatch(value):
        return value
    return None


async def helper_failure(response):
    """The helper's ``error`` word and optional ``reason``.

    ``upstream_http_429`` is Grok's own status. ``login_rejected`` is a 401/403
    from the account RPC. ``client_update_required`` is that same status with
    the update hint. A hibernated box stays ``upstream_http_417`` with no
    reason. Bodies are not logged.
    """
    try:
        body = await response.json(content_type=None)
    except Exception:
        body = None
    error = _safe_word(body.get("error") if isinstance(body, dict) else None)
    reason = _safe_word(body.get("reason") if isinstance(body, dict) else None)
    return error or f"http_{response.status}", reason


def update_event(message, agent_id, call_id):
    if not isinstance(message, dict) or not isinstance(message.get("payload"), dict):
        return None
    payload = message["payload"]
    if payload.get("agentId") != agent_id or payload.get("callId") != call_id:
        return None
    channel = message.get("channel")
    # voice-call-nudge and box-signal carry no result text. The transcript does.
    if channel in {"voice-call-nudge", "box-signal"}:
        return None
    if channel == "box-reply":
        text, topic = payload.get("text"), "work_landed"
        entry_id = payload.get("entryId")
        timestamp = payload.get("atMs")
        if (
            not isinstance(entry_id, str)
            or not 0 < len(entry_id) <= 200
            or type(timestamp) not in {int, float}
            or not math.isfinite(timestamp)
            or timestamp <= 0
        ):
            return None
        event_id, is_user = entry_id, False
        source = "transcript"
    elif channel == "typed-message":
        text, topic = payload.get("text"), "user_message"
        entry_id = payload.get("entryId")
        timestamp = payload.get("atMs")
        if (
            not isinstance(entry_id, str)
            or not 0 < len(entry_id) <= 200
            or type(timestamp) not in {int, float}
            or not math.isfinite(timestamp)
            or timestamp <= 0
        ):
            return None
        event_id, is_user = "written-turn-" + entry_id, True
        source = None
    else:
        return None  # Overheard is a different native protocol, not a completed task.
    if not isinstance(text, str) or not text.strip() or len(text) > 64000:
        return None
    event = {
        "type": "async_update.add",
        "event_id": event_id,
        "async_update": {
            "topic": topic,
            "payload": [
                {
                    "message": {"type": "text", "content": text},
                    "timestampMs": timestamp,
                }
            ],
        },
    }
    if source:
        event["desktop_entry"] = event_id
        event["desktop_source"] = source
    return event, is_user


class NativeVoiceChannel:
    def __init__(self, http, base_url, agent_id, call_id, on_update, on_gap=None, on_signal=None):
        self._http, self._url = http, base_url
        self.agent_id, self.call_id = agent_id, call_id
        self._on_update = on_update
        self._on_gap = on_gap
        self._on_signal = on_signal
        self._task = None
        self._closed = False
        self._registered = False
        self._presence_owned = False
        self._retry_delay = 5.0
        self._typed_seen = set()
        self._presence_status = None
        self._error = None
        self._reason = None
        self._ready_mono = None
        self._call_logged = False
        self.connected = asyncio.Event()

    async def _presence(self, on):
        async with self._http.post(
            self._url + "/native/presence",
            json={
                "agentId": self.agent_id,
                "callId": self.call_id,
                "isOnTheLine": on,
                "acceptsOverheard": False,
            },
            allow_redirects=False,
        ) as response:
            # Hangup must not erase the registration status the portal watches.
            if on:
                self._presence_status = response.status
            if response.status != 200:
                error, reason = await helper_failure(response)
                if on:
                    self._error = error
                    self._reason = reason
                logger.info("Grok native presence on=%s status=%d error=%s", on, response.status, error)
                if reason in {"login_rejected", "client_update_required"}:
                    raise RuntimeError(reason)
                raise RuntimeError("native_presence_unavailable")
            self._reason = None
            logger.info("Grok native presence on=%s status=%d", on, response.status)
            if on and not self._call_logged:
                self._call_logged = True
                logger.info("desktop call start id=%s", self.call_id)
            result = await response.json()
            if result.get("ok") is False:
                raise RuntimeError("native_presence_rejected")

    @property
    def ready(self):
        return not self._closed and self._registered and self.connected.is_set()

    def within_reconnect_grace(self) -> bool:
        """True when this channel was up a moment ago and is trying again.

        ``_ready_mono`` is the last moment the stream was up, refreshed when it
        drops. A stream that stayed up for minutes still gets the grace window.
        """
        if self._closed or self._ready_mono is None:
            return False
        return (time.monotonic() - self._ready_mono) <= RECONNECT_RECENT_S

    def health(self):
        """Desktop readiness for voice-health. Same condition as native_not_ready."""
        ready = self.ready
        return {
            "state": "ready" if ready else "unavailable",
            "ready": ready,
            "presence_status": self._presence_status,
            "error": None if ready else self._error,
            "reason": None if ready else self._reason,
        }

    async def start(self):
        # Native sandbox availability must not hold the independent audio socket.
        _open_channels.add(self)
        self._task = asyncio.create_task(self._run(), name="gbot-native-events")

    async def _run(self):
        while not self._closed:
            try:
                self._presence_owned = True
                await self._presence(True)
                self._registered = True
                await self._listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not isinstance(exc, RuntimeError):
                    self._error = type(exc).__name__
                logger.warning(
                    "Grok native channel state=unavailable retry=pending reason=%s error=%s",
                    str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__,
                    self._error,
                )
            finally:
                was_ready = self.connected.is_set()
                self._registered = False
                self.connected.clear()
                if was_ready:
                    # Last moment it was up, so a long-lived stream still gets grace.
                    self._ready_mono = time.monotonic()
                    if not self._closed and self._on_gap is not None:
                        asyncio.create_task(self._on_gap(), name="desktop-stream-gap")
            if not self._closed:
                # A stream that was up and then ended is the ~150s clock (or a
                # routine socket drop). Reconnect without the 5s backoff.
                # Presence and HTTP failures still wait.
                if not was_ready:
                    await asyncio.sleep(self._retry_delay)

    async def accept_event(self, message):
        if (
            isinstance(message, dict)
            and message.get("channel") == "box-signal"
            and isinstance(message.get("payload"), dict)
            and message["payload"].get("agentId") == self.agent_id
            and message["payload"].get("callId") == self.call_id
            and message["payload"].get("kind") in NUDGE_SIGNAL_KINDS
        ):
            if not self._closed and self._on_signal is not None:
                await self._on_signal()
            return
        parsed = update_event(message, self.agent_id, self.call_id)
        if self._closed or parsed is None:
            return
        event, is_user = parsed
        if is_user:
            if event["event_id"] in self._typed_seen or len(self._typed_seen) >= 2048:
                return
            self._typed_seen.add(event["event_id"])
        await self._on_update(event, is_user)

    async def _listen(self):
        query = urlencode({"agentId": self.agent_id, "callId": self.call_id})
        async with self._http.get(
            self._url + "/native/events?" + query,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60),
        ) as response:
            if response.status != 200:
                self._error, self._reason = await helper_failure(response)
                raise RuntimeError("native_events_unavailable")
            self._error = None
            self._reason = None
            self._ready_mono = time.monotonic()
            self.connected.set()
            logger.info("Grok native channel state=ready")
            data, length = [], 0
            async for raw in response.content:
                if self._closed:
                    break
                line = raw.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data:
                        await self.accept_event(json.loads("\n".join(data)))
                    data, length = [], 0
                elif line.startswith("data:"):
                    length += len(line)
                    if length > 70000:
                        raise ValueError("native_event_too_large")
                    data.append(line[5:].lstrip())

    async def close(self):
        _open_channels.discard(self)
        self._closed = True
        self.connected.clear()
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._registered = False
        if self._presence_owned:
            self._presence_owned = False
            try:
                await asyncio.wait_for(self._presence(False), 3)
            except Exception:
                pass
