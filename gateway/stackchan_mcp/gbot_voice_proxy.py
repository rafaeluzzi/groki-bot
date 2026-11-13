# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Opt-in Grok Bot voice, retaining the established device audio transport."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time

from .desktop_tasks import handoff_already_closed, log_desktop_task
from .gbot_voice_bridge import GbotVoiceBridge
from .gemini_voice_proxy import GeminiVoiceProxy
from .koji_wiki import publish_bridge_presence
from .panel_frames import panel_action_delivery

logger = logging.getLogger(__name__)

# Debounce is 0.4s. Anything older than this cannot still be that second tap.
_PANEL_TAP_KEEP_S = 5.0


@dataclass
class GbotVoiceProxy(GeminiVoiceProxy):
    health_check_interval_s: float = 30.0
    health_check_timeout_s: float = 10.0
    connect_timeout_s: float = 40.0
    reconnect_initial_delay_s: float = 1.0
    reconnect_max_delay_s: float = 30.0
    _health_task: asyncio.Task | None = field(default=None, init=False)
    _desktop_unsaid: list = field(default_factory=list, init=False)
    _panel_taps: dict = field(default_factory=dict, init=False)
    _health: dict = field(default_factory=lambda: {
        "provider": "gbot_voice", "state": "stopped", "reconnect_attempts": 0,
        "successful_checks": 0, "last_verified_at": None, "last_check_at": None,
        "last_error": None, "action_required": False,
    }, init=False)

    def health_snapshot(self):
        result = dict(self._health)
        if result["state"] == "connected" and not self.connected:
            result["state"] = "reconnecting"
        # Desktop 502s stay on `native` so voice `ok` can remain connected.
        result["native"] = self._native_health()
        return result

    def _native_health(self):
        bridge = self._bridge
        channel = getattr(bridge, "_native", None) if bridge is not None else None
        report = getattr(channel, "health", None)
        if callable(report):
            snapshot = report()
            if isinstance(snapshot, dict):
                return snapshot
        return {"state": "unavailable", "ready": False, "presence_status": None, "error": None, "reason": None}

    def _record_connection(self, connected, error=None):
        self._health["state"] = "connected" if connected else "reconnecting"
        self._health["last_error"] = error
        self._health["action_required"] = error in {
            "login_unavailable", "login_expired_open_grok_bot", "unsupported_storage",
            "upstream_http_401", "upstream_http_403",
        }
        if connected:
            self._health["last_verified_at"] = time.time()
        publish_bridge_presence(online=bool(connected))

    async def start(self, info, device_hello, send_to_device):
        if self._health_task is not None:
            await self.stop()
        self._status.voice_health_provider = self.health_snapshot
        self._health["state"] = "connecting"
        try:
            connected = await self._try_connect(info, device_hello, send_to_device)
        except asyncio.CancelledError:
            await super().stop()
            self._health["state"] = "stopped"
            publish_bridge_presence(online=False)
            raise
        self._record_connection(connected, None if connected else self._health["last_error"])
        self._health_task = asyncio.create_task(
            self._supervise(info, device_hello, send_to_device), name="gbot-voice-health"
        )
        return connected

    async def _try_connect(self, info, device_hello, send_to_device):
        try:
            connected = await asyncio.wait_for(
                super().start(info, device_hello, send_to_device), self.connect_timeout_s
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Grok voice connection failed (details redacted)")
            connected = False
        if not connected:
            self._health["last_error"] = (
                getattr(self._bridge, "metrics", {}).get("connection_error")
                or "connection_failed"
            )
            await self._abort_in_flight_tts()
            await super().stop()
            self._stopped = False
        return connected

    async def handle_panel_action(self, frame):
        """Inject one panel tap into the live Grok session, or log why it was dropped.

        Does not open a session. A second tap on the same control inside 0.4s
        is ``dropped(debounce)``.
        """
        panel = frame.get("id") if isinstance(frame, dict) and isinstance(frame.get("id"), str) else "-"
        action = frame.get("action") if isinstance(frame, dict) and isinstance(frame.get("action"), str) else "-"
        panel = panel[:80]
        action = action[:80]
        send = getattr(self._bridge, "send_text", None) if self._bridge is not None else None
        live = bool(self.connected and callable(send))
        text, outcome = panel_action_delivery(frame, session_live=live)
        if text is None:
            logger.info("panel_action panel=%s action=%s dropped(%s)", panel, action, outcome)
            return
        now = time.monotonic()
        self._prune_panel_taps(now)
        key = (panel, action)
        previous = self._panel_taps.get(key)
        self._panel_taps[key] = now
        if previous is not None and now - previous < 0.4:
            logger.info("panel_action panel=%s action=%s dropped(debounce)", panel, action)
            return
        try:
            await send(text)
        except Exception:
            logger.info("panel_action panel=%s action=%s dropped(send_failed)", panel, action)
            return
        logger.info("panel_action panel=%s action=%s delivered", panel, action)
        logger.info("panel outcome id=%s result=tapped action=%s", panel, action)

    def _prune_panel_taps(self, now):
        """Drop debounce keys that can no longer match. Called when a tap is recorded."""
        stale = [key for key, seen in self._panel_taps.items() if now - seen > _PANEL_TAP_KEEP_S]
        for key in stale:
            del self._panel_taps[key]

    async def send_device_json(self, message):
        """A typed hand-off arrives as ``listen`` / ``detect`` text. Grok hears that sentence."""
        if isinstance(message, dict) and message.get("type") == "panel_action":
            await self.handle_panel_action(message)
            return True
        if (
            isinstance(message, dict)
            and message.get("type") == "listen"
            and message.get("state") == "detect"
        ):
            text = str(message.get("text") or "").strip()
            send = getattr(self._bridge, "send_text", None)
            if text and callable(send):
                await send(text)
                return True
        return await super().send_device_json(message)

    async def _supervise(self, info, device_hello, send_to_device):
        delay = self.reconnect_initial_delay_s
        checked_at = asyncio.get_running_loop().time()
        disconnected_at = None
        while not self._stopped:
            if self.connected:
                disconnected_at = None
                await asyncio.sleep(min(1.0, self.health_check_interval_s))
                if self._stopped:
                    return
                now = asyncio.get_running_loop().time()
                if self.connected and now - checked_at < self.health_check_interval_s:
                    continue
                if self.connected:
                    self._health["last_check_at"] = time.time()
                    if await self._bridge.check_connection(self.health_check_timeout_s):
                        self._health["successful_checks"] += 1
                        self._record_connection(True)
                        checked_at = asyncio.get_running_loop().time()
                        delay = self.reconnect_initial_delay_s
                        continue
                    self._health["last_error"] = "voice_check_failed"
                    if self._bridge.metrics.get("connection_error") == "response_stalled":
                        self._health["last_error"] = "response_stalled"
            elif self.running:
                # A bridge may already be restoring its own transport/session.
                now = asyncio.get_running_loop().time()
                if disconnected_at is None:
                    disconnected_at = now
                if now - disconnected_at < self.connect_timeout_s:
                    await asyncio.sleep(min(1.0, self.health_check_interval_s))
                    continue
            self._record_connection(False, self._health["last_error"] or "connection_lost")
            await self._abort_in_flight_tts()
            self._take_desktop_work()
            await super().stop()
            # The supervisor owns recovery; explicit stop cancels this task.
            self._stopped = False
            await asyncio.sleep(self.reconnect_max_delay_s if self._health["action_required"] else delay)
            self._health["reconnect_attempts"] += 1
            connected = await self._try_connect(info, device_hello, send_to_device)
            self._record_connection(connected, None if connected else self._health["last_error"])
            if connected:
                checked_at = asyncio.get_running_loop().time()
                await self._flush_desktop_work()
            delay = min(self.reconnect_max_delay_s, delay * 2)

    def _take_desktop_work(self):
        bridge = self._bridge
        take = getattr(bridge, "take_desktop_work", None)
        if callable(take):
            self._desktop_unsaid.extend(take())

    async def _flush_desktop_work(self):
        bridge = self._bridge
        say = getattr(bridge, "say_desktop_later", None)
        if not callable(say):
            return
        pending, self._desktop_unsaid = self._desktop_unsaid, []
        still = []
        for item in pending:
            try:
                ok = await say(item)
            except Exception:
                logger.warning("desktop task handoff failed id=%s", item.get("id"))
                ok = False
            if not ok:
                still.append(item)
        self._desktop_unsaid = still + self._desktop_unsaid

    def _abandon_desktop_work(self):
        for item in self._desktop_unsaid:
            # Quiet, timeout, and delivered already logged their outcome.
            # 19:15:04 wrote outcome=failed again for c7516f0be274 and df6e899db105.
            if handoff_already_closed(item):
                continue
            log_desktop_task(
                str(item.get("id")),
                str(item.get("start")),
                "failed",
                waited_ms=int(item.get("waited_ms") or 0),
            )
        self._desktop_unsaid.clear()

    async def stop(self):
        task, self._health_task = self._health_task, None
        self._stopped = True
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._take_desktop_work()
        self._abandon_desktop_work()
        await self._abort_in_flight_tts()
        await super().stop()
        self._health["state"] = "stopped"
        publish_bridge_presence(online=False)

    def _voice_api_key(self):
        return None

    def _voice_requires_key(self):
        return False

    def _voice_requires_device(self):
        return False

    def _voice_hello(self):
        return {"provider": "gbot_voice", "experimental": True}

    def _speak_waits_for_first_audio(self):
        return True

    def _voice_tts_mode(self):
        return "native"

    def _make_bridge(self, esp32, **kwargs):
        return (self.bridge_factory or GbotVoiceBridge)(
            esp32=esp32,
            tool_executor=self.voice_tool_executor,
            tool_executor_factory=self.voice_tool_executor_factory,
            tool_extensions=self.voice_tool_extensions,
            **kwargs,
        )
