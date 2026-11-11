"""xiaozhi cloud WebSocket proxy for StackChan voice traffic.

The ESP32 can keep a single WebSocket open to this local gateway.  This module
opens a second WebSocket to the xiaozhi cloud and forwards every non-local MCP
message plus binary Opus frames between both sides.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import websockets

from .debug_status import DebugStatus, get_debug_status

logger = logging.getLogger(__name__)

DEFAULT_CLOUD_URL = "wss://api.tenclass.net/xiaozhi/v1/"
_PROXY_ID_START = 1_000_000

SendToDevice = Callable[[str | bytes], Awaitable[None]]


@dataclass(frozen=True)
class CloudConnectionInfo:
    """Connection metadata copied from the ESP32 WebSocket request headers."""

    cloud_url: str = DEFAULT_CLOUD_URL
    authorization: str = ""
    protocol_version: str = "1"
    device_id: str = ""
    client_id: str = ""

    @property
    def enabled(self) -> bool:
        if os.getenv("XIAOZHI_CLOUD_PROXY", "1").lower() in {"0", "false", "no", "off"}:
            return False
        return bool(self.cloud_url and self.authorization)

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.authorization:
            headers["Authorization"] = self.authorization
        if self.protocol_version:
            headers["Protocol-Version"] = self.protocol_version
        if self.device_id:
            headers["Device-Id"] = self.device_id
        if self.client_id:
            headers["Client-Id"] = self.client_id
        return headers

    @classmethod
    def from_headers(cls, headers: Any) -> "CloudConnectionInfo":
        token = os.getenv("XIAOZHI_CLOUD_TOKEN", "")
        authorization = _normalise_authorization(token) if token else headers.get("Authorization", "")
        return cls(
            cloud_url=os.getenv("XIAOZHI_CLOUD_URL", DEFAULT_CLOUD_URL),
            authorization=authorization,
            protocol_version=headers.get("Protocol-Version", "1"),
            device_id=headers.get("Device-Id", ""),
            client_id=headers.get("Client-Id", ""),
        )


@dataclass
class CloudProxy:
    """Bidirectional proxy between one ESP32 connection and xiaozhi cloud."""

    voice_bridge: Any | None = None
    debug_status: DebugStatus | None = None
    connect_factory: Any = websockets.connect
    info: CloudConnectionInfo | None = None
    server_hello: dict[str, Any] | None = None
    _ws: Any = None
    _task: asyncio.Task[None] | None = None
    _send_to_device: SendToDevice | None = None
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _tts_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _next_proxy_id: int = _PROXY_ID_START
    _proxy_to_cloud_id: dict[int, Any] = field(default_factory=dict)
    _ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    _active_tts: dict[str, Any] | None = None
    _device_hello: dict[str, Any] | None = None

    @property
    def _status(self) -> DebugStatus:
        return self.debug_status or get_debug_status()

    @property
    def connected(self) -> bool:
        return bool(self._ws is not None and not getattr(self._ws, "closed", False))

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        info: CloudConnectionInfo,
        device_hello: dict[str, Any],
        send_to_device: SendToDevice,
    ) -> bool:
        """Start cloud proxy in the background.

        Returns False when proxying is intentionally disabled, for example when
        the ESP32 didn't provide a Bearer token.  A failed cloud connection is
        logged by the background task and doesn't break local MCP.
        """
        self.info = info
        self._send_to_device = send_to_device
        self._device_hello = device_hello
        if not info.enabled:
            logger.info("xiaozhi cloud proxy disabled or missing auth; local MCP remains available")
            return False
        if self.running:
            await self._wait_until_ready()
            return True
        self._ready_event.clear()
        self._task = asyncio.create_task(self._run(device_hello), name="xiaozhi-cloud-proxy")
        await self._wait_until_ready()
        return True

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._proxy_to_cloud_id.clear()
        self._status.on_tts_state(False, source="xiaozhi")
        self._status.on_tts_state(False, source="xiaozhi_request")

    async def ensure_connected(self) -> bool:
        """Reconnect the xiaozhi cloud socket if it closed while ESP32 stayed online."""
        if self.connected:
            return True
        if self.info is None or self._device_hello is None or self._send_to_device is None:
            return False
        if not self.info.enabled:
            return False
        if self.running:
            await self._wait_until_ready()
            return self.connected

        self._ready_event.clear()
        self._task = asyncio.create_task(self._run(self._device_hello), name="xiaozhi-cloud-proxy")
        await self._wait_until_ready()
        return self.connected

    async def handle_panel_action(self, frame: dict[str, Any]) -> None:
        """Xiaozhi cloud is not the Grok session. A tap is dropped, not forwarded."""
        panel = frame.get("id") if isinstance(frame.get("id"), str) else "-"
        action = frame.get("action") if isinstance(frame.get("action"), str) else "-"
        logger.info(
            "panel_action panel=%s action=%s dropped(no_session)",
            panel[:80],
            action[:80],
        )

    async def send_device_json(self, message: dict[str, Any]) -> bool:
        """Forward a JSON message from ESP32 to xiaozhi cloud."""
        if not await self.ensure_connected():
            return False
        await self._send_cloud(json.dumps(message, ensure_ascii=False))
        return True

    async def send_device_binary(self, data: bytes) -> bool:
        """Forward an Opus binary frame from ESP32 to xiaozhi cloud."""
        if not await self.ensure_connected():
            return False
        await self._send_cloud(data)
        return True

    async def speak_text(
        self,
        text: str,
        *,
        session_id: str,
        prompt_audio_frames: list[bytes],
        emotion: str | None = None,
    ) -> dict[str, Any]:
        """Ask xiaozhi cloud to synthesize text and stream the reply to ESP32."""
        if not await self.ensure_connected():
            return {
                "ok": False,
                "text": text,
                "emotion": emotion,
                "frames_sent": 0,
                "provider": "xiaozhi_cloud",
                "error": "xiaozhi cloud websocket is not connected",
            }

        async with self._tts_lock:
            done = asyncio.Event()
            state: dict[str, Any] = {
                "text": text,
                "emotion": emotion,
                "frames_sent": 0,
                "started": False,
                "done": done,
                "error": None,
            }
            self._active_tts = state
            self._status.on_tts_state(True, source="xiaozhi_request")
            try:
                if emotion and self._send_to_device is not None:
                    await self._send_to_device(json.dumps({"type": "llm", "emotion": emotion}, ensure_ascii=False))
                await self._send_cloud(
                    json.dumps(
                        {"session_id": session_id, "type": "listen", "state": "start", "mode": "auto"},
                        ensure_ascii=False,
                    )
                )
                for frame in prompt_audio_frames:
                    await self._send_cloud(frame)
                    await asyncio.sleep(0.06)
                await self._send_cloud(
                    json.dumps({"session_id": session_id, "type": "listen", "state": "stop"}, ensure_ascii=False)
                )
                await self._send_cloud(b"")
                timeout = float(os.getenv("XIAOZHI_CLOUD_TTS_TIMEOUT_SECONDS", "45"))
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                state["error"] = "xiaozhi cloud TTS timed out"
            except Exception as exc:
                state["error"] = str(exc)
            finally:
                self._active_tts = None
                self._status.on_tts_state(False, source="xiaozhi_request")

            return {
                "ok": not state["error"] and state["frames_sent"] > 0,
                "text": text,
                "emotion": emotion,
                "frames_sent": state["frames_sent"],
                "provider": "xiaozhi_cloud",
                "error": state["error"]
                or (None if state["frames_sent"] > 0 else "xiaozhi cloud returned no audio frames"),
            }

    async def try_handle_device_mcp(self, message: dict[str, Any]) -> bool:
        """Forward ESP32 MCP responses that belong to cloud-originated calls.

        Cloud JSON-RPC ids are remapped before sending to the ESP32 to avoid
        collisions with local gateway MCP requests.  When the ESP32 replies, this
        restores the original cloud id and forwards the response upstream.
        """
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return False
        msg_id = payload.get("id")
        if not isinstance(msg_id, int) or msg_id not in self._proxy_to_cloud_id:
            return False
        cloud_id = self._proxy_to_cloud_id.pop(msg_id)
        restored = dict(message)
        restored_payload = dict(payload)
        restored_payload["id"] = cloud_id
        restored["payload"] = restored_payload
        return await self.send_device_json(restored)

    async def _run(self, device_hello: dict[str, Any]) -> None:
        assert self.info is not None
        try:
            async with self.connect_factory(
                self.info.cloud_url,
                additional_headers=self.info.headers(),
            ) as ws:
                self._ws = ws
                logger.info(
                    "xiaozhi cloud proxy connected: device=%s protocol=%s",
                    self.info.device_id or "unknown",
                    self.info.protocol_version,
                )
                await self._send_cloud(json.dumps(device_hello, ensure_ascii=False))
                self._ready_event.set()
                async for message in ws:
                    if isinstance(message, bytes):
                        await self._forward_cloud_binary(message)
                    else:
                        await self._handle_cloud_json(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("xiaozhi cloud proxy unavailable: %s", exc)
            self._ready_event.set()
        finally:
            self._ws = None
            self._status.on_tts_state(False, source="xiaozhi")
            self._status.on_tts_state(False, source="xiaozhi_request")
            self._ready_event.set()

    async def _wait_until_ready(self) -> None:
        timeout = float(os.getenv("XIAOZHI_CLOUD_CONNECT_TIMEOUT_SECONDS", "8"))
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "xiaozhi cloud proxy connect timed out after %.1fs; local MCP remains available",
                timeout,
            )

    async def _handle_cloud_json(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from xiaozhi cloud: %s", raw[:100])
            return

        msg_type = message.get("type")
        if msg_type == "hello":
            self.server_hello = message
            logger.info("xiaozhi cloud hello received")
            return

        self._observe_tts_json(message)
        tts_state = message.get("state") if msg_type == "tts" else None
        if tts_state == "start":
            self._status.on_tts_state(True, source="xiaozhi")
        if self._active_tts is not None and msg_type == "stt":
            logger.debug("suppressing prompt STT during local speak request")
            return
        if msg_type == "stt" and self.voice_bridge is not None and getattr(self.voice_bridge, "enabled", False):
            try:
                await self.voice_bridge.handle_stt_message(message)
            except Exception as exc:
                logger.warning("voice input bridge dropped STT fail-closed: %s", exc)
            return

        if msg_type == "mcp":
            message = self._remap_cloud_mcp_request(message)

        await self._forward_cloud_json(message)
        if tts_state == "stop":
            self._status.on_tts_state(False, source="xiaozhi")

    def _remap_cloud_mcp_request(self, message: dict[str, Any]) -> dict[str, Any]:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return message
        # Only requests have method+id. Notifications have no id; responses have
        # no method.  Leave both unchanged.
        if "method" not in payload or "id" not in payload:
            return message
        proxy_id = self._next_proxy_id
        self._next_proxy_id += 1
        self._proxy_to_cloud_id[proxy_id] = payload["id"]
        remapped = dict(message)
        remapped_payload = dict(payload)
        remapped_payload["id"] = proxy_id
        remapped["payload"] = remapped_payload
        return remapped

    async def _forward_cloud_json(self, message: dict[str, Any]) -> None:
        if self._send_to_device is None:
            return
        await self._send_to_device(json.dumps(message, ensure_ascii=False))

    async def _forward_cloud_binary(self, data: bytes) -> None:
        if self._active_tts is not None:
            self._active_tts["frames_sent"] += 1
        if self._send_to_device is None:
            return
        await self._send_to_device(data)

    async def _send_cloud(self, message: str | bytes) -> None:
        if self._ws is None:
            raise ConnectionError("xiaozhi cloud websocket is not connected")
        async with self._send_lock:
            await self._ws.send(message)

    def _observe_tts_json(self, message: dict[str, Any]) -> None:
        active = self._active_tts
        if active is None:
            return
        if message.get("type") != "tts":
            if message.get("type") in {"error", "alert"}:
                active["error"] = message.get("message") or message.get("text") or json.dumps(message, ensure_ascii=False)
                active["done"].set()
            return

        state = message.get("state")
        if state == "start":
            active["started"] = True
        elif state == "stop":
            active["done"].set()


def _normalise_authorization(token_or_header: str) -> str:
    token_or_header = token_or_header.strip()
    if not token_or_header:
        return ""
    if " " in token_or_header:
        return token_or_header
    return f"Bearer {token_or_header}"
