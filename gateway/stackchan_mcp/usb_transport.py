"""USB Serial/JTAG MCP transport for StackChan.

Speaks the same protocol as firmware/main/usb_mcp_endpoint.cc:
  • Lines beginning with "$MCP:" carry JSON-RPC payloads.
  • All other lines are device-side ESP_LOG output. We forward those to a
    log file so the device stays observable while the gateway holds the
    only serial handle (macOS lets a single process own /dev/cu.usbmodem*).

The transport is asyncio-friendly even though pyserial itself is blocking:
a background daemon thread owns the serial handle, lines flow back to the
asyncio loop via run_coroutine_threadsafe, and call_tool() awaits the
response future keyed on the JSON-RPC id.

The reader thread also implements hot-plug recovery: when the device
disconnects (USB cable yanked, firmware reboot), Serial.readline raises
SerialException, the thread loops back to discovery, and start()'s
connected event reflects the live state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import serial
from serial.tools import list_ports

logger = logging.getLogger(__name__)


class UsbToolError(RuntimeError):
    """Raised when the firmware MCP server returns a JSON-RPC error response.

    Without it a tools/call that returned {"error": {...}} would look like a
    success: TrackingBridge would think the head moved and never fall back to
    WS. The generic except in TrackingBridge._send_head_angles catches this
    exception and uses the WebSocket fallback.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"USB tool error {code}: {message}")


# ESP32-S3 USB Serial/JTAG controller default identifiers.
ESP32S3_USB_JTAG_VID = 0x303A
ESP32S3_USB_JTAG_PID = 0x1001

MCP_PREFIX = "$MCP:"
DEFAULT_BAUD = 115200
DEFAULT_RECONNECT_DELAY_S = 1.0
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_LOG_PATH = Path(os.path.expanduser("~/.stackchan/usb.log"))


def discover_port(
    vid: int = ESP32S3_USB_JTAG_VID,
    pid: int = ESP32S3_USB_JTAG_PID,
) -> str | None:
    """Return the first /dev/cu.usbmodem* matching (vid, pid), or None.

    Matching by USB ID (not by /dev path) keeps the gateway robust to
    enumeration order changes — usbmodem101 today may be usbmodem301
    after a reboot.
    """
    for descriptor in list_ports.comports():
        if descriptor.vid == vid and descriptor.pid == pid:
            return descriptor.device
    return None


class UsbTransport:
    """Single-device USB MCP transport bound to one asyncio loop."""

    def __init__(
        self,
        *,
        port: str | None = None,
        baud: int = DEFAULT_BAUD,
        log_path: Path | None = None,
        reconnect_delay_s: float = DEFAULT_RECONNECT_DELAY_S,
        serial_factory: Any = None,
    ) -> None:
        """Create the transport.

        port: pin to a specific /dev/cu.* device. None = auto-discover by VID/PID.
        log_path: where to spool ESP_LOG lines. None = ~/.stackchan/usb.log.
        serial_factory: dependency injection for tests; defaults to serial.Serial.
        """
        self._port = port
        self._baud = baud
        self._log_path = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
        self._reconnect_delay = reconnect_delay_s
        self._serial_factory = serial_factory or serial.Serial

        self._serial: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock: asyncio.Lock | None = None
        self._connected_event: asyncio.Event | None = None
        self._log_file: Any | None = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def connected(self) -> bool:
        return self._connected_event is not None and self._connected_event.is_set()

    async def start(self, *, wait_connect: bool = True, connect_timeout_s: float = 5.0) -> bool:
        """Open the serial port and start the read loop.

        Returns True if the device connected within connect_timeout_s,
        False otherwise (the reader keeps retrying in the background).
        """
        if self._reader_thread is not None:
            return self.connected
        self._loop = asyncio.get_running_loop()
        self._send_lock = asyncio.Lock()
        self._connected_event = asyncio.Event()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = self._log_path.open("a", encoding="utf-8")
        self._stop_flag.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="usb-mcp-reader",
            daemon=True,
        )
        self._reader_thread.start()

        if not wait_connect:
            return self.connected
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=connect_timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        """Tear down the read loop and close the serial port."""
        self._stop_flag.set()
        ser = self._serial
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        if self._reader_thread is not None:
            await asyncio.to_thread(self._reader_thread.join, 2.0)
            self._reader_thread = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    # ------------------------------------------------------------------ rpc

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send tools/call and await the JSON-RPC response.

        Raises UsbToolError when the firmware returns a JSON-RPC error
        envelope (e.g. unknown tool, validation failure). The exception is
        the trigger for the WS fallback in TrackingBridge; without it a
        firmware-side error looks like success and we'd never retry.
        """
        request = {
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        data = await self._send_rpc(request, timeout_s=timeout_s)
        self._raise_if_error(data)
        return data

    async def list_tools(self, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        """Send tools/list and await the JSON-RPC response."""
        data = await self._send_rpc(
            {"method": "tools/list", "params": {}},
            timeout_s=timeout_s,
        )
        self._raise_if_error(data)
        return data

    async def initialize(self, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        """Send initialize handshake. Useful as a connectivity smoke test."""
        data = await self._send_rpc(
            {"method": "initialize", "params": {"capabilities": {}}},
            timeout_s=timeout_s,
        )
        self._raise_if_error(data)
        return data

    @staticmethod
    def _raise_if_error(data: dict[str, Any]) -> None:
        err = data.get("error") if isinstance(data, dict) else None
        if not isinstance(err, dict):
            return
        raise UsbToolError(
            code=int(err.get("code", -1)),
            message=str(err.get("message", "unknown USB tool error")),
            data=err.get("data"),
        )

    async def _send_rpc(
        self,
        rpc: dict[str, Any],
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        if self._loop is None or self._send_lock is None:
            raise RuntimeError("UsbTransport.start() not awaited yet")

        rid = self._next_id
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": rid, **rpc}
        frame = MCP_PREFIX + " " + json.dumps(body, ensure_ascii=False) + "\n"
        fut = self._loop.create_future()
        self._pending[rid] = fut
        try:
            async with self._send_lock:
                ser = self._serial
                if ser is None or not self.connected:
                    raise RuntimeError("USB transport not connected")
                await asyncio.to_thread(ser.write, frame.encode("utf-8"))
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise
        except Exception:
            self._pending.pop(rid, None)
            raise

    # ------------------------------------------------------------------ reader

    def _reader_loop(self) -> None:
        """Background thread: open port, read lines, hot-plug reconnect."""
        while not self._stop_flag.is_set():
            target_port = self._port or discover_port()
            if target_port is None:
                self._sleep_or_stop(self._reconnect_delay)
                continue

            try:
                ser = self._serial_factory(target_port, self._baud, timeout=0.2)
            except Exception as exc:
                logger.warning("USB serial open(%s) failed: %s", target_port, exc)
                self._sleep_or_stop(self._reconnect_delay)
                continue

            self._serial = ser
            self._signal_event(self._connected_event_set)
            logger.info("USB transport connected on %s", target_port)
            try:
                self._read_until_eof(ser)
            except Exception as exc:
                logger.info("USB transport read loop ended: %s", exc)
            finally:
                self._serial = None
                self._signal_event(self._connected_event_clear)
                # Fail every in-flight RPC immediately on disconnect
                # so callers (TrackingBridge) skip the 300 ms USB timeout and
                # fall back to WS within the same tick. Without this a yanked
                # cable + new face frame waits the full timeout AND lets the
                # firmware execute a stale command twice (USB + WS).
                self._signal_event(self._fail_pending_due_to_disconnect)
                try:
                    ser.close()
                except Exception:
                    pass

            if self._stop_flag.is_set():
                break
            self._sleep_or_stop(self._reconnect_delay)

    def _read_until_eof(self, ser: Any) -> None:
        while not self._stop_flag.is_set():
            line = ser.readline()
            if not line:
                continue
            try:
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            except Exception:
                continue
            if not text:
                continue
            if text.startswith(MCP_PREFIX):
                self._dispatch_response_line(text)
            else:
                self._append_log(text)

    def _dispatch_response_line(self, line: str) -> None:
        payload = line[len(MCP_PREFIX):].lstrip()
        try:
            data = json.loads(payload)
        except Exception:
            logger.warning("USB transport bad JSON: bytes=%s", len(payload.encode("utf-8")))
            return
        rid = data.get("id")
        if rid is None:
            return
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._resolve_pending, rid, data)

    def _resolve_pending(self, rid: int, data: dict[str, Any]) -> None:
        fut = self._pending.pop(rid, None)
        if fut is not None and not fut.done():
            fut.set_result(data)

    def _fail_pending_due_to_disconnect(self) -> None:
        """Drain _pending with ConnectionError. Runs in the asyncio loop."""
        if not self._pending:
            return
        err = ConnectionError("USB transport disconnected")
        # Snapshot the dict to avoid mutation-during-iteration.
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(err)
                # If the caller is concurrently unwinding through a write
                # error / timeout path, asyncio may otherwise log "Future
                # exception was never retrieved" even though the transport is
                # deliberately failing fast for WS fallback.
                fut.exception()
            self._pending.pop(rid, None)

    def _append_log(self, text: str) -> None:
        if self._log_file is None:
            return
        try:
            self._log_file.write(text + "\n")
            self._log_file.flush()
        except Exception:
            pass

    def _signal_event(self, fn: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(fn)

    def _connected_event_set(self) -> None:
        if self._connected_event is not None:
            self._connected_event.set()

    def _connected_event_clear(self) -> None:
        if self._connected_event is not None:
            self._connected_event.clear()

    def _sleep_or_stop(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop_flag.is_set():
                return
            time.sleep(min(0.1, end - time.monotonic()))
