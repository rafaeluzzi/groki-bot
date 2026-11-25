"""ESP32 connection manager.

Acts as a WebSocket server that ESP32 connects TO,
and as an MCP client that sends commands TO the ESP32.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
import websockets.exceptions
from websockets.asyncio.server import ServerConnection

from .audio_stream import SpeakResult, handle_audio_frame, speak_text
from .cloud_proxy import CloudConnectionInfo, CloudProxy
from .core_frames import core_ack_timeout_s, is_thin_core_frame
from .device_commands import is_device_command_frame
from .koji_wiki import record_portal_presence
from .panel_frames import is_panel_frame
from .debug_status import DebugStatus, get_debug_status
from .protocol import HelloResponse, make_mcp_message, parse_jsonrpc_response

logger = logging.getLogger(__name__)

# Omitted seq means "read the context at send time". A tool passes the seq it saw at dispatch.
_TURN_SEQ_UNSET = object()

# Timeout for waiting for ESP32 responses
RESPONSE_TIMEOUT = 300.0


def _require_cloud_speak() -> bool:
    """Return whether speak must use the xiaozhi cloud proxy.

    Default is False so speak uses gateway-side TTS and preserves the exact
    input text instead of asking the cloud LLM to repeat it.
    """
    return os.getenv("STACKCHAN_REQUIRE_CLOUD", "0").lower() in {"1", "true", "yes", "on"}


class ESP32Connection:
    """Manages a single ESP32 device connection."""

    def __init__(self, ws: ServerConnection, session_id: str):
        self._ws = ws
        self.session_id = session_id
        self.device_id: str = "unknown"
        self.tools: list[dict[str, Any]] = []
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._core_waiters: list[tuple[str, asyncio.Future[dict[str, Any]]]] = []
        self._connected = True
        self._initialized = False
        # Device-declared WebSocket protocol version (from the hello
        # message). Defaults to 1, which matches the firmware's default
        # (firmware/main/protocols/websocket_protocol.h: ``version_ = 1``)
        # and the audio framing this gateway emits today (raw Opus
        # payload). v2/v3 add a BinaryProtocol header that this gateway
        # does not yet wrap — see Issue follow-up to #70.
        self.protocol_version: int = 1
        self.cloud_proxy: Any | None = None
        self._speak_lock = asyncio.Lock()
        # Device-declared MCP support (from the hello ``features.mcp``
        # flag). Some chassis firmwares (e.g. ciniml/stackchan-idf's
        # XiaoZhiClient) speak the same xiaozhi hello/listen/tts dialect
        # but carry no MCP client at all — they send ``features.mcp:
        # false`` and never answer an "mcp" envelope. Defaults to True so
        # a connection built outside the hello handshake (as in tests)
        # keeps the historical "assume full MCP" behaviour.
        self.mcp_supported: bool = True
        self.one_voice: bool = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def initialized(self) -> bool:
        return self._initialized

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def send_mcp_request(
        self, method: str, params: dict[str, Any]
    ) -> tuple[Any, dict[str, Any] | None]:
        """Send an MCP request to ESP32 and wait for response.

        Returns (result, error).
        """
        if not self._connected:
            return None, {"code": -32000, "message": "ESP32 not connected"}

        req_id = self._next_id()
        message = make_mcp_message(self.session_id, method, params, req_id)

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            await self._ws.send(json.dumps(message))
            response = await asyncio.wait_for(future, timeout=RESPONSE_TIMEOUT)
            return parse_jsonrpc_response(response)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return None, {"code": -32000, "message": f"Timeout waiting for ESP32 response (method={method})"}
        except Exception as exc:
            self._pending.pop(req_id, None)
            return None, {"code": -32000, "message": f"ESP32 communication error: {exc}"}

    async def initialize(self, vision_url: str = "", vision_token: str = "") -> bool:
        """Send MCP initialize to ESP32."""
        capabilities: dict[str, Any] = {}
        if vision_url:
            vision: dict[str, Any] = {"url": vision_url}
            if vision_token:
                vision["token"] = vision_token
            capabilities["vision"] = vision
        result, error = await self.send_mcp_request("initialize", {"capabilities": capabilities})
        if error:
            logger.error("ESP32 initialize failed: %s", error)
            return False

        logger.info(
            "ESP32 initialized: protocol=%s server=%s",
            result.get("protocolVersion", "?"),
            result.get("serverInfo", {}),
        )
        self._initialized = True
        return True

    async def discover_tools(self) -> list[dict[str, Any]]:
        """Discover tools available on ESP32."""
        all_tools: list[dict[str, Any]] = []
        cursor = ""

        while True:
            params: dict[str, Any] = {"cursor": cursor}
            result, error = await self.send_mcp_request("tools/list", params)

            if error:
                logger.error("tools/list failed: %s", error)
                break

            tools = result.get("tools", [])
            all_tools.extend(tools)

            next_cursor = result.get("nextCursor", "")
            if not next_cursor:
                break
            cursor = next_cursor

        self.tools = all_tools
        logger.info("Discovered %d tools on ESP32", len(all_tools))
        return all_tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[Any, dict[str, Any] | None]:
        """Call a tool on ESP32."""
        if not self.mcp_supported:
            return None, {
                "code": -32000,
                "message": "ESP32 MCP unsupported (features.mcp=false)",
            }
        return await self.send_mcp_request(
            "tools/call", {"name": name, "arguments": arguments}
        )

    async def send_emotion(self, emotion: str) -> dict[str, Any]:
        """Send a XiaoZhi ``llm.emotion`` message over the live WebSocket."""
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        message = {
            "session_id": self.session_id,
            "type": "llm",
            "emotion": emotion,
        }
        await self._ws_send(json.dumps(message, ensure_ascii=False))
        return {"ok": True, "emotion": emotion}

    async def send_led(self, r: int, g: int, b: int) -> dict[str, Any]:
        """Send a XiaoZhi ``led`` message over the live WebSocket."""
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        message = {
            "type": "led",
            "r": max(0, min(255, int(r))),
            "g": max(0, min(255, int(g))),
            "b": max(0, min(255, int(b))),
        }
        await self._ws_send(json.dumps(message))
        return {"ok": True, "r": message["r"], "g": message["g"], "b": message["b"]}

    async def send_head(self, yaw: int, pitch: int, speed: int) -> dict[str, Any]:
        """Send a XiaoZhi ``head`` message over the live WebSocket."""
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        message = {
            "type": "head",
            "yaw": max(-90, min(90, int(yaw))),
            "pitch": max(0, min(60, int(pitch))),
            "speed": max(100, min(1000, int(speed))),
        }
        await self._ws_send(json.dumps(message))
        return {
            "ok": True,
            "yaw": message["yaw"],
            "pitch": message["pitch"],
            "speed": message["speed"],
        }

    async def speak(self, text: str, emotion: str | None = None) -> SpeakResult:
        """Queue one TTS utterance and stream it to the ESP32."""

        async def _set_avatar(face: str) -> None:
            await self.call_tool("self.display.set_avatar", {"face": face})

        async with self._speak_lock:
            return await speak_text(
                self._ws.send,
                text,
                emotion=emotion,
                set_avatar=_set_avatar,
                cloud_proxy=self.cloud_proxy,
                session_id=self.session_id,
                require_cloud=_require_cloud_speak(),
            )

    def handle_response(self, payload: dict[str, Any]) -> bool:
        """Handle an incoming MCP response from ESP32."""
        req_id = payload.get("id")
        if req_id is not None and req_id in self._pending:
            future = self._pending.pop(req_id)
            if not future.done():
                future.set_result(payload)
            return True
        else:
            # Notification (no id) — log and discard for now
            method = payload.get("method", "")
            logger.info("ESP32 notification: %s", method)
            return False

    async def _ws_send(self, payload: bytes | str) -> None:
        """Send a payload, translating websockets errors to ConnectionError.

        The ``websockets`` library raises its own exception hierarchy
        (``ConnectionClosed`` and friends), which is *not* a subclass
        of the built-in :class:`ConnectionError`. Without translation
        the orchestrator's ``except ConnectionError`` filter — and the
        MCP handler's ``except RuntimeError`` filter — would let those
        errors leak as raw tracebacks into the MCP transport, breaking
        the say() tool's clean error JSON contract on mid-stream
        disconnect.
        """
        try:
            await self._ws.send(payload)
        except (
            websockets.exceptions.ConnectionClosed,
            OSError,
        ) as exc:
            # Mark the connection dead so subsequent calls fail fast
            # rather than each one re-discovering the broken socket.
            self.disconnect()
            raise ConnectionError(f"WebSocket send failed: {exc}") from exc

    async def send_audio_frame(self, opus_frame: bytes) -> None:
        """Send a single Opus frame to the ESP32 as a WebSocket binary frame.

        The device's ``OnData`` handler (firmware/main/protocols/
        websocket_protocol.cc) treats every binary frame as an Opus
        audio payload to feed into its decoder, so this method is the
        TTS pipeline's egress point.
        """
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        await self._ws_send(opus_frame)

    async def send_tts_state(self, state: str) -> None:
        """Send a TTS state notification (``start`` / ``stop`` / ...).

        The device's :func:`Application::OnIncomingJson` translates
        ``{"type":"tts","state":"start"}`` into
        :data:`kDeviceStateSpeaking`, which is the gate for
        :func:`OnIncomingAudio` pushing packets into the decode queue
        (see ``firmware/main/application.cc``). Without bracketing the
        audio frames in start/stop, the device drops them on the floor
        and the speaker stays silent — the TTS tool returns success
        without anything actually playing.
        """
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        message = {
            "session_id": self.session_id,
            "type": "tts",
            "state": state,
        }
        await self._ws_send(json.dumps(message))

    async def send_core(self, frame: dict[str, Any], *, timeout_s: float = 3.0) -> dict[str, Any]:
        """Send a thin ``type:core`` frame and wait for the matching ack.

        Does not require ``features.mcp``. The payload is the frame as given;
        callers must already have built ``{"type","action", name|text}``.
        """
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        if not is_thin_core_frame(frame):
            raise ValueError("invalid core frame")
        action = str(frame["action"])
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._core_waiters.append((action, future))
        try:
            await self._ws_send(json.dumps(frame, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout_s)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Timeout waiting for core ack (action={action})") from None
        finally:
            self._core_waiters = [
                (item_action, item)
                for item_action, item in self._core_waiters
                if item is not future
            ]

    async def send_panel(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Send one ``type:panel`` frame. Portal does not ack it.

        The caller must already have built a valid frame. This writes that
        text frame only: it does not send ``abort`` and it does not wait,
        so opening or closing a panel does not stall the voice turn.
        """
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        if not is_panel_frame(frame):
            raise ValueError("invalid panel frame")
        await self._ws_send(json.dumps(frame, ensure_ascii=False))
        return {"ok": True}

    async def send_podcast(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Send one ``type:podcast`` frame. Portal speaks it and opens Spotify.

        There is no ack. The write does not send ``abort`` and does not wait.
        """
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        from .podcast_open import is_podcast_frame

        if not is_podcast_frame(frame):
            raise ValueError("invalid podcast frame")
        await self._ws_send(json.dumps(frame, ensure_ascii=False))
        return {"ok": True}

    async def send_device_command(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Send one ``type:device_command`` frame. Portal does not ack it.

        The caller must already have built a valid frame. This writes that
        text frame only: it does not send ``abort`` and it does not wait,
        so a volume or media command does not stall the voice turn.
        ``get_volume`` is included; a later ``device_command_result`` is not
        read here.
        """
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        if not is_device_command_frame(frame):
            raise ValueError("invalid device command frame")
        await self._ws_send(json.dumps(frame, ensure_ascii=False))
        return {"ok": True}

    def handle_core_ack(self, message: dict[str, Any]) -> bool:
        """Complete the oldest waiter for this action. Ignores other JSON."""
        if message.get("type") != "core":
            return False
        action = message.get("action")
        for index, (expected, future) in enumerate(self._core_waiters):
            if expected == action and not future.done():
                future.set_result(message)
                del self._core_waiters[index]
                return True
        return False

    def disconnect(self) -> None:
        """Mark connection as disconnected."""
        self._connected = False
        self._initialized = False
        # Cancel all pending futures
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("ESP32 disconnected"))
        self._pending.clear()
        waiters = self._core_waiters
        self._core_waiters = []
        for _action, future in waiters:
            if not future.done():
                future.set_exception(ConnectionError("ESP32 disconnected"))


class ESP32Manager:
    """Manages ESP32 device connections.

    Runs a WebSocket server that ESP32 devices connect to.
    Currently supports a single device connection.
    """

    def __init__(
        self,
        cloud_proxy_factory: Any | None = None,
        usb_transport: Any | None = None,
        local_tool_handler: Callable[[str, dict[str, Any]], Any] | None = None,
        debug_status: DebugStatus | None = None,
    ):
        self._debug_status = debug_status or get_debug_status()
        self._connection: ESP32Connection | None = None
        self._server: Any = None
        self._lock = asyncio.Lock()
        self._init_tasks: list[asyncio.Task] = []
        self._vision_url: str = ""
        self._vision_token: str = ""
        self.usb_transport = usb_transport
        # Per-device serialisation for TTS send sequences. Acquired by
        # the orchestrator around the entire start → frames → stop
        # block so concurrent ``say()`` invocations cannot interleave
        # their Opus frames on the same WebSocket or overlap their
        # ``tts.start``/``tts.stop`` notifications (which would yank
        # the firmware out of ``kDeviceStateSpeaking`` mid-utterance
        # and silently drop the remaining audio). The lock is scoped
        # to the manager because the manager owns the device today —
        # if multi-device support lands later, the lock should move
        # onto :class:`ESP32Connection` instead.
        self._tts_lock = asyncio.Lock()
        self._cloud_proxy_factory = cloud_proxy_factory or CloudProxy
        self._local_tool_handler = local_tool_handler
        self.on_activity: Callable[[], None] | None = None
        self.on_head_command: Callable[[], None] | None = None
        self.on_device_state: Callable[[str], None] | None = None
        self.on_wake_detected: Callable[[], Awaitable[None]] | None = None
        # Podcast turn key when the Portal has not sent a context seq.
        self.listen_starts = 0
        self.connection_generation = 0
        self.one_voice = False
        self.on_portal_hello: Callable[[], Any] | None = None
        self.koji_context: dict[str, Any] | None = None
        self.announce_podcast: Callable[..., Any] | None = None
        self._command_ids = 0
        from .portal_read import PortalStatusCache
        from .quiet_context import QuietContext

        self.portal_cache = PortalStatusCache()
        self.quiet = QuietContext()
        self.pending_portal_events: list[dict[str, Any]] = []
        self.on_portal_frame: Callable[[], Any] | None = None
        self._podcast_waiters: dict[int, list[asyncio.Future]] = {}
        self._command_waiters: dict[str, asyncio.Future] = {}
        self._last_podcast_outcome: dict[str, Any] | None = None

    @property
    def device_connected(self) -> bool:
        return self._connection is not None and self._connection.connected

    @property
    def mcp_supported(self) -> bool:
        """False only while a voice-only (features.mcp=false) device is connected."""
        conn = self._connection
        if conn is None or not conn.connected:
            return True
        return bool(getattr(conn, "mcp_supported", True))

    @property
    def usb_connected(self) -> bool:
        usb = self.usb_transport
        return usb is not None and bool(getattr(usb, "connected", False))

    @property
    def connection(self) -> ESP32Connection | None:
        return self._connection

    @property
    def tts_lock(self) -> asyncio.Lock:
        """Per-device lock guarding the TTS send sequence.

        See :attr:`_tts_lock` for the rationale; the orchestrator wraps
        the start → frames → stop block in ``async with`` on this lock.
        """
        return self._tts_lock

    def get_status_with_transports(self) -> dict[str, Any]:
        """Get status for callers that can use either WebSocket or USB."""
        status = dict(self.get_status())
        ws_connected = bool(status.get("connected"))
        usb_connected = self.usb_connected
        status["ws_connected"] = ws_connected
        status["usb_connected"] = usb_connected
        if usb_connected and not ws_connected:
            status["connected"] = True
            status["transport"] = "usb"
        elif ws_connected:
            status["transport"] = "websocket"
        else:
            status["transport"] = None
        return status

    async def start(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        vision_url: str = "",
        vision_token: str = "",
    ) -> None:
        """Start the WebSocket server for ESP32 connections."""
        self._vision_url = vision_url
        self._vision_token = vision_token
        logger.info("ESP32 WebSocket server starting on ws://%s:%d", host, port)
        self._server = await websockets.serve(
            self._handler,
            host,
            port,
            process_request=self._check_auth,
        )

    def note_device_hello(self) -> None:
        """A Portal socket came up. Drop the last snapshot and a turn that never finished."""
        self.portal_cache.clear()
        self.quiet.release_turn()

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        self.quiet.release_turn()
        # Cancel any pending initialization tasks
        for task in self._init_tasks:
            task.cancel()
        self._init_tasks.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _check_auth(
        self, connection: ServerConnection, request: websockets.http11.Request
    ) -> None | websockets.http11.Response:
        """Validate Bearer token.

        websockets 16+ passes (connection, request) to process_request.
        """
        expected = os.getenv("STACKCHAN_TOKEN") or os.getenv("BEARER_TOKEN")
        if not expected:
            logger.warning("STACKCHAN_TOKEN not set — accepting all connections")
            return None

        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {expected}":
            return None

        logger.warning("ESP32 auth rejected")
        return websockets.http11.Response(
            401, "Unauthorized", websockets.datastructures.Headers()
        )

    async def _handler(self, ws: ServerConnection) -> None:
        """Handle an incoming ESP32 WebSocket connection.

        Architecture: the message read loop runs continuously, dispatching
        MCP responses to pending futures. Initialization (initialize + tools/list)
        runs as a separate task so it doesn't block the read loop.
        """
        session_id = str(uuid.uuid4())
        request_headers = ws.request.headers if ws.request else {}
        device_id = request_headers.get("Device-Id", "unknown")
        logger.info("ESP32 connecting: device=%s", device_id)

        connection = ESP32Connection(ws, session_id)
        connection.device_id = device_id
        cloud_proxy: CloudProxy | None = self._cloud_proxy_factory()
        connection.cloud_proxy = cloud_proxy

        try:
            async for message in ws:
                if isinstance(message, bytes):
                    self._debug_status.on_device_audio()
                    await handle_audio_frame(
                        message,
                        session_id,
                        cloud_proxy=cloud_proxy,
                    )
                    continue

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from ESP32: %s", str(message)[:100])
                    continue

                msg_type = data.get("type", "")
                # A replaced socket can still deliver queued TTS events. Only
                # the current device may update its speaking state.
                if connection is self._connection:
                    self._observe_device_state(data)

                if msg_type == "hello":
                    # ESP32 hello handshake
                    features = data.get("features", {})
                    if not isinstance(features, dict):
                        features = {}
                    mcp_supported = bool(features.get("mcp"))
                    connection.mcp_supported = mcp_supported
                    connection.one_voice = features.get("one_voice") is True
                    # Copy the flag before the voice session starts, so its
                    # tool text matches this hello. A later hello refreshes it.
                    self.one_voice = connection.one_voice
                    self.note_device_hello()
                    self.connection_generation += 1
                    self._last_podcast_outcome = None
                    guard = getattr(self, "podcast_guard", None)
                    reset = getattr(guard, "reset", None)
                    if callable(reset):
                        reset()
                    if not mcp_supported:
                        # Voice-only chassis (e.g. ciniml/stackchan-idf's
                        # XiaoZhiClient): accept the connection for audio +
                        # xiaozhi control JSON, but never attempt MCP
                        # initialize/tools_list — the device has no MCP
                        # client to answer them, and RESPONSE_TIMEOUT would
                        # otherwise stall the init task for 300 s per
                        # connection for nothing.
                        logger.info(
                            "ESP32 hello without MCP support (features.mcp="
                            "false); accepting as voice-only, skipping "
                            "initialize/tools_list"
                        )

                    # Capture the device's WebSocket protocol version
                    # so callers (e.g. the TTS pipeline) can decide
                    # whether their wire format is compatible. The
                    # firmware accepts raw Opus only on v1; v2/v3 wrap
                    # the payload in a BinaryProtocol header.
                    raw_version = data.get("version", 1)
                    try:
                        connection.protocol_version = int(raw_version)
                    except (TypeError, ValueError):
                        connection.protocol_version = 1
                    if connection.protocol_version != 1:
                        logger.warning(
                            "ESP32 negotiated WebSocket protocol "
                            "version=%s; the gateway emits raw Opus "
                            "binary frames matching v1 only. TTS "
                            "calls (say) will be blocked at the "
                            "orchestrator until v2/v3 BinaryProtocol "
                            "header wrapping is implemented",
                            connection.protocol_version,
                        )

                    if cloud_proxy is not None:
                        cloud_info = CloudConnectionInfo.from_headers(request_headers)
                        await cloud_proxy.start(cloud_info, data, ws.send)

                    # Send hello response only after the cloud socket has had a
                    # chance to connect and receive the ESP32 hello.  The ESP32
                    # starts streaming wake-word Opus immediately after server
                    # hello; replying too early drops the first listen/audio
                    # frames while the upstream cloud WebSocket is still opening.
                    resp = HelloResponse(session_id=session_id)
                    await ws.send(resp.model_dump_json())

                    # Register connection
                    async with self._lock:
                        if self._connection is not None and self._connection is not connection:
                            if self._connection.connected:
                                logger.warning("Replacing existing ESP32 connection")
                            self._connection.disconnect()
                            # The old handler's finally no longer owns _connection,
                            # so release its device and local speaking sources now.
                            self._debug_status.on_tts_state(False, source="device")
                            self._debug_status.on_tts_state(False, source="local")
                        self._connection = connection
                        self.one_voice = connection.one_voice
                        self.koji_context = None
                        self._last_podcast_outcome = None
                        self._debug_status.on_device_connected(device_id)
                    hook = self.on_portal_hello
                    if callable(hook):
                        try:
                            refreshed = hook()
                            if inspect.isawaitable(refreshed):
                                await refreshed
                        except Exception:
                            logger.warning("portal hello tool refresh failed")
                    if connection.one_voice:
                        self._hold(self._resync_router_status())

                    if mcp_supported:
                        # Start initialization as a separate task so the read
                        # loop continues to pump messages (responses to
                        # initialize/tools_list)
                        task = asyncio.create_task(self._init_device(connection, device_id))
                        self._init_tasks.append(task)
                        task.add_done_callback(lambda t: self._init_tasks.remove(t) if t in self._init_tasks else None)
                    else:
                        logger.info("ESP32 ready (voice-only): device=%s", device_id)

                elif msg_type == "core":
                    # Portal acks a thin frame on this socket. Never hand the
                    # text to the voice session; it is not user speech.
                    if connection is self._connection:
                        connection.handle_core_ack(data)
                    continue

                elif msg_type == "panel":
                    # Portal does not ack panels. A panel frame is not user
                    # speech and must not enter the voice turn.
                    continue

                elif msg_type == "panel_action":
                    # A tap. The voice proxy injects it, or logs the drop.
                    # It is not forwarded as a raw cloud message.
                    panel_id = data.get("id") if isinstance(data.get("id"), str) else "-"
                    action_id = data.get("action") if isinstance(data.get("action"), str) else "-"
                    if connection is not self._connection or cloud_proxy is None:
                        logger.info(
                            "panel_action panel=%s action=%s dropped(no_session)",
                            panel_id[:80],
                            action_id[:80],
                        )
                        continue
                    handler = getattr(cloud_proxy, "handle_panel_action", None)
                    if not callable(handler):
                        logger.info(
                            "panel_action panel=%s action=%s dropped(no_handler)",
                            panel_id[:80],
                            action_id[:80],
                        )
                        continue
                    try:
                        await handler(data)
                    except Exception:
                        logger.info(
                            "panel_action panel=%s action=%s dropped(send_failed)",
                            panel_id[:80],
                            action_id[:80],
                        )
                    continue

                elif msg_type in {"portal_status", "portal_event", "action_final"}:
                    if connection is self._connection:
                        self._take_portal_frame(data)
                    continue

                elif msg_type == "device_command":
                    # Fire-and-forget Portal system commands. Not user speech.
                    continue

                elif msg_type == "device_command_result":
                    # The tool waiter reads this. It is never user speech.
                    if connection is self._connection:
                        self._take_command_result(data)
                    continue

                elif msg_type == "koji_context":
                    # Screen context for this ask. Stored, never forwarded.
                    # A user turn is open until the reply finishes, so status events wait.
                    self.quiet.hold_turn(time.monotonic())
                    if connection is self._connection:
                        from .koji_voice import store_koji_context

                        stored = store_koji_context(data)
                        if stored is not None:
                            self.koji_context = stored
                            if isinstance(stored.get("portal_state"), dict):
                                self.portal_cache.offer(stored["portal_state"], source="portal")
                            if isinstance(stored.get("portal_events"), list) and stored["portal_events"]:
                                self.quiet.offer(stored.get("portal_state"), stored["portal_events"])
                    continue

                elif msg_type == "podcast_result":
                    if connection is self._connection:
                        self._take_podcast_result(data)
                    continue

                elif msg_type == "koji_presence":
                    # Portal heartbeats are not user speech. A valid body is
                    # stored; the frame is never forwarded into the voice turn.
                    record_portal_presence(data)
                    continue

                elif msg_type == "mcp":
                    if connection is not self._connection:
                        await self._handle_external_mcp_request(ws, data, session_id)
                        continue

                    # MCP response from ESP32
                    if cloud_proxy is not None and await cloud_proxy.try_handle_device_mcp(data):
                        continue
                    payload = data.get("payload", {})
                    handled_locally = connection.handle_response(payload)
                    if not handled_locally and cloud_proxy is not None:
                        await cloud_proxy.send_device_json(data)

                else:
                    if cloud_proxy is not None:
                        forwarded = await cloud_proxy.send_device_json(data)
                        if forwarded:
                            logger.debug("ESP32 message type=%s forwarded to cloud", msg_type)
                            continue
                    logger.debug("ESP32 message type=%s (ignored)", msg_type)

        except websockets.exceptions.ConnectionClosed:
            logger.info("ESP32 disconnected: device=%s", device_id)
        finally:
            if cloud_proxy is not None:
                await cloud_proxy.stop()
            connection.disconnect()
            async with self._lock:
                if self._connection is connection:
                    self._connection = None
                    self._debug_status.on_device_disconnected()
                    self._debug_status.on_tts_state(False, source="local")
                    self._debug_status.on_tts_state(False, source="device")

    async def _init_device(self, connection: ESP32Connection, device_id: str) -> None:
        """Initialize MCP session with a newly connected device."""
        if await connection.initialize(
            vision_url=self._vision_url,
            vision_token=self._vision_token,
        ):
            await connection.discover_tools()
            logger.info(
                "ESP32 ready: device=%s tools=%d",
                device_id,
                len(connection.tools),
            )
        else:
            logger.error("ESP32 MCP initialization failed")

    async def _handle_external_mcp_request(
        self,
        ws: ServerConnection,
        data: dict[str, Any],
        session_id: str,
    ) -> None:
        """Relay a hook/automation MCP request to the connected ESP32.

        The WebSocket server primarily serves the ESP32 client, but lightweight
        hook processes also use it as a command socket.  Those clients do not
        send the ESP32 hello handshake; they send one or more MCP tool calls and
        expect JSON-RPC responses on the same socket.
        """
        payload = data.get("payload", {})
        req_id = payload.get("id")
        method = payload.get("method", "")
        response_payload: dict[str, Any]

        if method == "initialize":
            response_payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "stackchan-hook-bridge", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                },
            }
        elif method == "tools/list":
            response_payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": self._connection.tools if self._connection else [], "nextCursor": ""},
            }
        elif method == "tools/call":
            params = payload.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            if tool_name == "get_status":
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(self.get_status_with_transports(), ensure_ascii=False),
                        }
                    ],
                    "isError": False,
                }
                error = None
            elif self._local_tool_handler is not None and tool_name in {"say", "set_voice_mode", "gemini_say", "set_face_tracking"}:
                try:
                    local_result = self._local_tool_handler(
                        tool_name,
                        arguments if isinstance(arguments, dict) else {},
                    )
                    if inspect.isawaitable(local_result):
                        local_result = await local_result
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(local_result, ensure_ascii=False),
                            }
                        ],
                        "isError": False,
                    }
                    error = None
                except Exception as exc:
                    result = None
                    error = {"code": -32000, "message": str(exc)}
            elif tool_name == "speak":
                result, error = await self.speak(
                    str(arguments.get("text", "")) if isinstance(arguments, dict) else "",
                    str(arguments.get("emotion")) if isinstance(arguments, dict) and arguments.get("emotion") else None,
                )
            else:
                tool_args = arguments if isinstance(arguments, dict) else {}
                result, error = None, None
                if self.usb_connected:
                    try:
                        usb_response = await self.usb_transport.call_tool(tool_name, tool_args, timeout_s=2.0)
                        result = usb_response.get("result", usb_response)
                    except Exception as exc:
                        if self.device_connected:
                            logger.debug("external MCP USB %s failed; falling back to WS: %s", tool_name, exc)
                        else:
                            error = {"code": -32000, "message": str(exc)}
                if result is None and error is None:
                    result, error = await self.call_tool(tool_name, tool_args)
            if error:
                response_payload = {"jsonrpc": "2.0", "id": req_id, "error": error}
            else:
                response_payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
        else:
            response_payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        await ws.send(
            json.dumps(
                {
                    "session_id": data.get("session_id", session_id),
                    "type": "mcp",
                    "payload": response_payload,
                }
            )
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        notify_activity: bool = True,
        notify_head_command: bool = True,
    ) -> tuple[Any, dict[str, Any] | None]:
        """Call a tool on the connected ESP32 device."""
        if not self._connection or not self._connection.connected:
            return None, {"code": -32000, "message": "No ESP32 device connected"}
        if not self._connection.mcp_supported:
            return None, {
                "code": -32000,
                "message": "ESP32 MCP unsupported (features.mcp=false)",
            }
        if not self._connection.initialized:
            return None, {"code": -32000, "message": "ESP32 not initialized"}
        if notify_activity and self.on_activity is not None:
            self.on_activity()
        if (
            notify_head_command
            and name == "self.robot.set_head_angles"
            and self.on_head_command is not None
        ):
            self.on_head_command()
        return await self._connection.call_tool(name, arguments)

    async def send_emotion(
        self,
        emotion: str,
        *,
        notify_activity: bool = True,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Send a non-MCP expression update to the connected device."""
        if not self._connection or not self._connection.connected:
            return None, {"code": -32000, "message": "No ESP32 device connected"}
        if notify_activity and self.on_activity is not None:
            self.on_activity()
        try:
            return await self._connection.send_emotion(emotion), None
        except Exception as exc:
            return None, {"code": -32000, "message": str(exc)}

    async def send_led(
        self,
        r: int,
        g: int,
        b: int,
        *,
        notify_activity: bool = True,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Send a non-MCP LED update to the connected device."""
        if not self._connection or not self._connection.connected:
            return None, {"code": -32000, "message": "No ESP32 device connected"}
        if notify_activity and self.on_activity is not None:
            self.on_activity()
        try:
            return await self._connection.send_led(r, g, b), None
        except Exception as exc:
            return None, {"code": -32000, "message": str(exc)}

    async def send_head(
        self,
        yaw: int,
        pitch: int,
        speed: int,
        *,
        notify_activity: bool = True,
        notify_head_command: bool = True,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Send a non-MCP head update to the connected device."""
        if not self._connection or not self._connection.connected:
            return None, {"code": -32000, "message": "No ESP32 device connected"}
        if notify_activity and self.on_activity is not None:
            self.on_activity()
        if notify_head_command and self.on_head_command is not None:
            self.on_head_command()
        try:
            return await self._connection.send_head(yaw, pitch, speed), None
        except Exception as exc:
            return None, {"code": -32000, "message": str(exc)}

    async def send_core(
        self,
        frame: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Send a thin Portal ``type:core`` frame on the current device socket.

        Works while ``features.mcp`` is false. One device at a time: a replaced
        socket cannot complete this call.
        """
        connection = self._connection
        if connection is None or not connection.connected:
            return None, {"code": -32000, "message": "No ESP32 device connected"}
        if not is_thin_core_frame(frame):
            return None, {"code": -32602, "message": "invalid core frame"}
        wait_s = core_ack_timeout_s() if timeout_s is None else timeout_s
        try:
            ack = await connection.send_core(frame, timeout_s=wait_s)
        except Exception as exc:
            return None, {"code": -32000, "message": str(exc)}
        if self._connection is not connection or not connection.connected:
            return None, {"code": -32000, "message": "ESP32 disconnected"}
        return ack, None

    async def send_panel(
        self,
        frame: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Send a Portal ``type:panel`` frame on the current device socket.

        Works while ``features.mcp`` is false. There is no panel ack: ok
        means the frame passed validation and was written. One device at a
        time: a replaced socket cannot complete this call.
        """
        connection = self._connection
        if connection is None or not connection.connected:
            return None, {"code": -32000, "message": "No ESP32 device connected"}
        outgoing = frame
        if self.one_voice and "id" not in outgoing:
            outgoing = dict(frame)
            outgoing["id"] = self.alloc_command_id()
        if not is_panel_frame(outgoing):
            return None, {"code": -32602, "message": "invalid panel frame"}
        try:
            result = await connection.send_panel(outgoing)
        except Exception as exc:
            return None, {"code": -32000, "message": str(exc)}
        if self._connection is not connection or not connection.connected:
            return None, {"code": -32000, "message": "ESP32 disconnected"}
        if self.one_voice and isinstance(result, dict):
            action_id = outgoing.get("id") if isinstance(outgoing.get("id"), str) else ""
            show_id = None
            dismiss_id = None
            panel = outgoing.get("panel") if isinstance(outgoing.get("panel"), dict) else None
            if isinstance(panel, dict) and isinstance(panel.get("id"), str):
                show_id = panel["id"]
            elif outgoing.get("dismiss") is True:
                current = {}
                cached = self.portal_cache.status if isinstance(self.portal_cache.status, dict) else {}
                panel_doc = cached.get("panel") if isinstance(cached.get("panel"), dict) else {}
                current = panel_doc.get("current") if isinstance(panel_doc.get("current"), dict) else {}
                if isinstance(current.get("id"), str):
                    dismiss_id = current["id"]
            confirmed = await self._wait_panel(show_id=show_id, dismiss_id=dismiss_id)
            result = dict(result)
            result["confirmed"] = confirmed
            if not confirmed:
                from .portal_state import mark_stale

                stale = mark_stale(self.portal_cache.status)
                if isinstance(stale, dict):
                    result["portal_state"] = stale
            result = self._finish_portal(
                result,
                action_id=action_id,
                state="done" if confirmed else "uncertain",
            )[0]
        return result, None

    async def _wait_panel(self, *, show_id: str | None, dismiss_id: str | None) -> bool:
        from .portal_state import panel_matches

        if not show_id and not dismiss_id:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.5
        while True:
            if panel_matches(self.portal_cache.status, show_id=show_id, dismiss_id=dismiss_id):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    def _hold(self, coro):
        task = asyncio.create_task(coro)
        held = getattr(self, "_held", None)
        if held is None:
            held = set()
            self._held = held
        held.add(task)
        task.add_done_callback(held.discard)
        return task

    def _arm_podcast_waiter(self, seq: int) -> asyncio.Future:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._podcast_waiters.setdefault(seq, []).append(future)
        return future

    def _drop_podcast_waiter(self, seq: object, future: asyncio.Future | None) -> None:
        if future is None or not isinstance(seq, int):
            return
        waiting = [item for item in self._podcast_waiters.get(seq, []) if item is not future]
        if waiting:
            self._podcast_waiters[seq] = waiting
        else:
            self._podcast_waiters.pop(seq, None)

    def _arm_command_waiter(self, command_id: str) -> asyncio.Future:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._command_waiters[command_id] = future
        return future

    def _drop_command_waiter(self, command_id: str, future: asyncio.Future | None) -> None:
        if future is None:
            return
        if self._command_waiters.get(command_id) is future:
            self._command_waiters.pop(command_id, None)

    def _take_podcast_result(self, message: dict[str, Any]) -> None:
        from .koji_voice import final_instruction

        seq = message.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            return
        phase = message.get("phase")
        show = message.get("show") if isinstance(message.get("show"), str) else ""
        if phase == "accepted":
            status = message.get("status") if isinstance(message.get("status"), str) else ""
            for future in self._podcast_waiters.pop(seq, []):
                if not future.done():
                    future.set_result({"status": status, "show": show})
            return
        if phase != "final":
            return
        outcome = message.get("outcome") if isinstance(message.get("outcome"), str) else ""
        if outcome in {"spotify_playing", "cancelled"}:
            self._last_podcast_outcome = {"outcome": outcome, "show": show}
            return
        text = final_instruction(outcome, show)
        if text is None:
            return
        announce = self.announce_podcast
        if not callable(announce):
            return
        result = announce(text)
        if inspect.isawaitable(result):
            asyncio.create_task(result)

    def _take_command_result(self, message: dict[str, Any]) -> None:
        command_id = message.get("id")
        if not isinstance(command_id, str) or not command_id:
            return
        future = self._command_waiters.pop(command_id, None)
        if future is None or future.done():
            return
        detail = message.get("detail") if isinstance(message.get("detail"), str) else ""
        body: dict[str, Any] = {"ok": message.get("ok") is True, "detail": detail}
        state = message.get("portal_state")
        if isinstance(state, dict):
            body["portal_state"] = state
            self.portal_cache.offer(state, source="portal")
        for key in ("action_id", "state"):
            if isinstance(message.get(key), str):
                body[key] = message[key]
        if message.get("deduped") is True:
            body["deduped"] = True
        future.set_result(body)

    def alloc_command_id(self) -> str:
        self._command_ids += 1
        return f"cmd-{self._command_ids}"

    def _finish_portal(
        self,
        body: dict[str, Any],
        *,
        action_id: str,
        state: str,
        portal_state: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], None]:
        from .portal_state import attach_portal

        out = dict(body)
        if isinstance(portal_state, dict):
            self.portal_cache.offer(portal_state, source="portal")
            out["portal_state"] = portal_state
        return attach_portal(self, out, action_id=action_id, state=state), None

    async def send_grok_state(self, frame: dict[str, Any]) -> None:
        """Progress only. A missing socket or a non-one-voice device drops it."""
        if not self.one_voice:
            return
        connection = self._connection
        if connection is None or not connection.connected:
            return
        try:
            await connection._ws_send(json.dumps(frame, ensure_ascii=False))
        except Exception:
            logger.info("grok_state dropped")

    def _take_portal_frame(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        status = message.get("status") if kind == "portal_status" else message.get("portal_state")
        if isinstance(status, dict):
            self.portal_cache.offer(status, source="portal")
        events = message.get("events") if isinstance(message.get("events"), list) else []
        if kind == "action_final":
            action = message.get("action") if isinstance(message.get("action"), dict) else {}
            events = [
                {
                    "type": "action_final",
                    "action_id": action.get("action_id") if isinstance(action.get("action_id"), str) else "",
                    "state": action.get("state") if isinstance(action.get("state"), str) else "",
                }
            ]
        # A heartbeat or any other status with no events only refreshes the cache.
        logger.info("portal_frame type=%s", kind if kind in {"portal_status", "portal_event", "action_final"} else "other")
        if not events:
            return
        self.quiet.offer(self.portal_cache.status, events)
        hook = self.on_portal_frame
        if callable(hook):
            try:
                result = hook()
                if inspect.isawaitable(result):
                    self._hold(result)
            except Exception:
                self.quiet.fail()
                self.pending_portal_events = self.quiet.take_attached()
        else:
            self.quiet.fail()
            self.pending_portal_events = self.quiet.take_attached()

    async def _resync_router_status(self) -> None:
        from .portal_read import fetch_router_status

        try:
            body = await fetch_router_status()
        except Exception:
            logger.info("portal_state resync ok=false")
            return
        status = body.get("status") if isinstance(body, dict) else None
        if self.portal_cache.offer(status, source="router"):
            logger.info("portal_state resync ok=true source=router")
        else:
            logger.info("portal_state resync ok=true source=kept")

    async def send_podcast(
        self,
        frame: dict[str, Any],
        turn_seq: Any = _TURN_SEQ_UNSET,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Send a Portal ``type:podcast`` frame on the current device socket.

        There is no ack: ok means the frame was written. In one-voice mode the
        Portal stays silent and Grok says the line. ``turn_seq`` is the ask
        seq from when the tool started.
        """
        connection = self._connection
        if connection is None or not connection.connected:
            return None, {"code": -32000, "message": "No ESP32 device connected"}
        from .koji_voice import ACCEPT_WAIT_S, accept_result, context_seq, podcast_target, say_hint, stamp_podcast_frame
        from .podcast_open import is_podcast_frame

        if turn_seq is _TURN_SEQ_UNSET:
            seq = context_seq(self.koji_context)
        elif isinstance(turn_seq, int) and not isinstance(turn_seq, bool):
            seq = turn_seq
        else:
            seq = None
        stamped = stamp_podcast_frame(frame, one_voice=self.one_voice, seq=seq)
        if self.one_voice:
            stamped["id"] = self.alloc_command_id()
        if not is_podcast_frame(stamped):
            return None, {"code": -32602, "message": "invalid podcast frame"}
        show = str(stamped.get("showTitle") or "")
        waiter = None
        if self.one_voice and isinstance(stamped.get("seq"), int):
            waiter = self._arm_podcast_waiter(stamped["seq"])
        try:
            result = await connection.send_podcast(stamped)
        except Exception as exc:
            self._drop_podcast_waiter(stamped.get("seq"), waiter)
            return None, {"code": -32000, "message": str(exc)}
        if self._connection is not connection or not connection.connected:
            self._drop_podcast_waiter(stamped.get("seq"), waiter)
            return None, {"code": -32000, "message": "ESP32 disconnected"}
        if not self.one_voice:
            return result, None
        last = self._last_podcast_outcome
        self._last_podcast_outcome = None
        action_id = stamped.get("id") if isinstance(stamped.get("id"), str) else ""
        if waiter is None:
            return self._finish_portal(accept_result("sent_unconfirmed", show, stamped, last), action_id=action_id, state="uncertain")
        try:
            accepted = await asyncio.wait_for(waiter, ACCEPT_WAIT_S)
        except asyncio.TimeoutError:
            self._drop_podcast_waiter(stamped.get("seq"), waiter)
            return self._finish_portal(accept_result("sent_unconfirmed", show, stamped, last), action_id=action_id, state="uncertain")
        status = accepted.get("status") if isinstance(accepted, dict) else None
        if not isinstance(status, str) or not status:
            status = "sent_unconfirmed"
        heard = accepted.get("show") if isinstance(accepted, dict) else None
        if isinstance(heard, str) and heard.strip():
            show = heard
        body = accept_result(status, show, stamped, last)
        if status == "sent_unconfirmed":
            body["say"] = say_hint("sent_unconfirmed", show)
            body["target"] = podcast_target(stamped)
        phase = "in_progress" if status == "opening" else "done"
        portal = accepted.get("portal_state") if isinstance(accepted, dict) else None
        return self._finish_portal(body, action_id=action_id, state=phase, portal_state=portal)

    async def send_device_command(
        self,
        frame: dict[str, Any],
        turn_seq: Any = _TURN_SEQ_UNSET,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Send a Portal ``type:device_command`` frame on the current device socket.

        Works while ``features.mcp`` is false. There is no ack: ok means the
        frame passed validation and was written. One device at a time: a
        replaced socket cannot complete this call. ``get_volume`` does not
        wait for ``device_command_result``.
        """
        connection = self._connection
        if connection is None or not connection.connected:
            return None, {"code": -32000, "message": "No ESP32 device connected"}
        from .koji_voice import ACCEPT_WAIT_S, context_seq, stamp_device_command

        if turn_seq is _TURN_SEQ_UNSET:
            seq = context_seq(self.koji_context)
        elif isinstance(turn_seq, int) and not isinstance(turn_seq, bool):
            seq = turn_seq
        else:
            seq = None
        command_id = ""
        if self.one_voice:
            command_id = self.alloc_command_id()
        stamped = stamp_device_command(frame, one_voice=self.one_voice, seq=seq, command_id=command_id)
        if not is_device_command_frame(stamped):
            return None, {"code": -32602, "message": "invalid device command frame"}
        waiter = None
        if self.one_voice and command_id:
            waiter = self._arm_command_waiter(command_id)
        try:
            result = await connection.send_device_command(stamped)
        except Exception as exc:
            self._drop_command_waiter(command_id, waiter)
            return None, {"code": -32000, "message": str(exc)}
        if self._connection is not connection or not connection.connected:
            self._drop_command_waiter(command_id, waiter)
            return None, {"code": -32000, "message": "ESP32 disconnected"}
        if waiter is None:
            return result, None
        try:
            accepted = await asyncio.wait_for(waiter, ACCEPT_WAIT_S)
        except asyncio.TimeoutError:
            self._drop_command_waiter(command_id, waiter)
            from .portal_state import mark_stale

            stale = mark_stale(self.portal_cache.status)
            missed: dict[str, Any] = {"ok": True, "confirmed": False}
            if stale is not None:
                missed["portal_state"] = stale
            return missed, None
        detail = accepted.get("detail") if isinstance(accepted, dict) else ""
        ok = bool(isinstance(accepted, dict) and accepted.get("ok") is True)
        heard: dict[str, Any] = {
            "ok": ok,
            "confirmed": True,
            "detail": detail if isinstance(detail, str) else "",
        }
        if isinstance(accepted, dict):
            for key in ("portal_state", "action_id", "state"):
                if key in accepted:
                    heard[key] = accepted[key]
            if accepted.get("deduped") is True:
                heard["deduped"] = True
        return heard, None

    async def send_audio_frame(self, opus_frame: bytes) -> None:
        """Push a single Opus frame to the connected device.

        Used by the TTS pipeline to deliver synthesised audio. Raises
        :class:`ConnectionError` if no device is currently attached so
        the orchestrator can surface a clean error to the MCP client
        instead of silently dropping audio.
        """
        if not self._connection or not self._connection.connected:
            raise ConnectionError("No ESP32 device connected")
        await self._connection.send_audio_frame(opus_frame)

    async def send_tts_state(self, state: str) -> None:
        """Send a TTS state notification (``start`` / ``stop`` / ...).

        Required around audio frame egress so the device transitions
        into ``kDeviceStateSpeaking`` and back; see
        :meth:`ESP32Connection.send_tts_state` for the full rationale.
        """
        connection = self._connection
        if not connection or not connection.connected:
            raise ConnectionError("No ESP32 device connected")
        self._observe_outgoing_tts_state(state)
        if state == "start":
            self._debug_status.on_tts_state(True, source="local")
        try:
            await connection.send_tts_state(state)
        except BaseException:
            if state == "start" and self._connection is connection:
                self._debug_status.on_tts_state(False, source="local")
            raise
        finally:
            if state == "stop" and self._connection is connection:
                self._debug_status.on_tts_state(False, source="local")

    async def speak(self, text: str, emotion: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Speak through the connected ESP32 device."""
        if not self._connection or not self._connection.connected:
            return None, {"code": -32000, "message": "No ESP32 device connected"}
        if not self._connection.initialized:
            return None, {"code": -32000, "message": "ESP32 not initialized"}
        if self.on_activity is not None:
            self.on_activity()
        result = await self._connection.speak(text, emotion)
        if not result.ok:
            return result.to_dict(), {"code": -32000, "message": result.error or "speak failed"}
        return result.to_dict(), None

    def _observe_device_state(self, message: dict[str, Any]) -> None:
        callback = self.on_device_state
        msg_type = message.get("type", "")
        state = str(message.get("state", "")).lower()
        if msg_type == "listen":
            if state == "start":
                self.listen_starts += 1
            if state in {"start", "detect"}:
                if callback is not None:
                    callback("listening")
                if state == "detect" and self.on_wake_detected is not None:
                    asyncio.create_task(self.on_wake_detected())
            elif state == "stop":
                if callback is not None:
                    callback("idle")
        elif msg_type == "tts":
            if state == "start":
                self._debug_status.on_tts_state(True, source="device")
                if callback is not None:
                    callback("speaking")
            elif state == "stop":
                self._debug_status.on_tts_state(False, source="device")
                if callback is not None:
                    callback("idle")

    def _observe_outgoing_tts_state(self, state: str) -> None:
        callback = self.on_device_state
        if callback is None:
            return
        normalized = state.lower()
        if normalized == "start":
            callback("speaking")
        elif normalized == "stop":
            callback("idle")

    def get_status(self) -> dict[str, Any]:
        """Get current connection status."""
        if not self._connection or not self._connection.connected:
            return {
                "connected": False,
                "device_id": None,
                "tools_count": 0,
            }
        return {
            "connected": True,
            "device_id": self._connection.device_id,
            "initialized": self._connection.initialized,
            "mcp_supported": self._connection.mcp_supported,
            "voice_proxy": (
                "connected"
                if self._connection.cloud_proxy is not None
                and getattr(self._connection.cloud_proxy, "connected", False)
                else "inactive"
            ),
            "tools_count": len(self._connection.tools),
            "tools": [t.get("name", "") for t in self._connection.tools],
        }
