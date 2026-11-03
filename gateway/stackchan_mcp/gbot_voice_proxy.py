# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Opt-in Grok Bot voice, retaining the established device audio transport."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time

from .gbot_voice_bridge import GbotVoiceBridge
from .gemini_voice_proxy import GeminiVoiceProxy
from .koji_wiki import publish_bridge_presence

logger = logging.getLogger(__name__)


@dataclass
class GbotVoiceProxy(GeminiVoiceProxy):
    health_check_interval_s: float = 30.0
    health_check_timeout_s: float = 10.0
    connect_timeout_s: float = 40.0
    reconnect_initial_delay_s: float = 1.0
    reconnect_max_delay_s: float = 30.0
    _health_task: asyncio.Task | None = field(default=None, init=False)
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
        return {"state": "unavailable", "ready": False, "presence_status": None}

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
            await super().stop()
            # The supervisor owns recovery; explicit stop cancels this task.
            self._stopped = False
            await asyncio.sleep(self.reconnect_max_delay_s if self._health["action_required"] else delay)
            self._health["reconnect_attempts"] += 1
            connected = await self._try_connect(info, device_hello, send_to_device)
            self._record_connection(connected, None if connected else self._health["last_error"])
            if connected:
                checked_at = asyncio.get_running_loop().time()
            delay = min(self.reconnect_max_delay_s, delay * 2)

    async def stop(self):
        task, self._health_task = self._health_task, None
        self._stopped = True
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
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
