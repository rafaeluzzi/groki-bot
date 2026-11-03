# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Thin XiaoZhi ``type:device_command`` frames for Portal system controls.

The Grok login-voice path declares ``device_command`` locally and forwards a
call as one JSON text frame on the device WebSocket. The frame does not use
MCP, so it still works while hello advertises ``features.mcp: false``. It
uses the same socket write as ``type:panel`` and does not open a second
transport.

Portal owns system volume, brightness, media transport, and opening an app.
The voice instructions tell the model to call this tool for those requests
and not to change Mac volume or a desktop player.

Tool names (case-insensitive; dots become underscores):

| Call name | Frame |
|---|---|
| ``device_command`` | ``{"type":"device_command","name":"...","args":{...}}`` |
| ``portal_device_command``, ``self.device.command`` | same frame, session-catalog aliases |

Only ``device_command`` is declared. The aliases stay on the local forwarder
when a session catalog lists them, so they are not posted to the desktop
harness.

Catalog v1 ``name`` values and the ``args`` object are checked before the
frame is sent. ``args`` is always present; commands with no parameters use
``{}``. The wire contract is ``gateway/PORTAL_DEVICE_COMMANDS.md``. Portal's
device handlers were not in this repo when this was written, so that note is
the contract to implement against.

Portal does not need to ack the frame (same as ``type:panel``). The tool
returns ok after validation and the write. ``get_volume`` is on-device only:
the gateway does not wait for ``device_command_result`` and does not return a
number. Inbound ``device_command`` and ``device_command_result`` text is not
user speech and is not forwarded into the voice turn.

Set ``STACKCHAN_DEVICE_COMMANDS=0`` to stop declaring the tool on the gbot
path. The default is on, same as ``STACKCHAN_PANEL_FRAMES``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

# Catalog v1. Keep this tuple, the tool description, and
# gateway/PORTAL_DEVICE_COMMANDS.md in sync.
DEVICE_COMMAND_NAMES = (
    "set_volume_percent",
    "set_volume_level",
    "volume_up",
    "volume_down",
    "get_volume",
    "media_pause",
    "media_resume",
    "media_stop",
    "media_skip_next",
    "media_skip_previous",
    "set_brightness_percent",
    "brightness_up",
    "brightness_down",
    "open_app",
    "show_settings",
    "reconnect_bridge",
)

_PERCENT_COMMANDS = frozenset({"set_volume_percent", "set_brightness_percent"})
_LEVEL_COMMANDS = frozenset({"set_volume_level"})
_APP_COMMANDS = frozenset({"open_app"})
_NO_ARG_COMMANDS = frozenset(DEVICE_COMMAND_NAMES) - _PERCENT_COMMANDS - _LEVEL_COMMANDS - _APP_COMMANDS

_CANONICAL_NAMES = {name: name for name in DEVICE_COMMAND_NAMES}

# Normalized spellings a session catalog might use. Command names are arguments,
# not tool names: ``open_app`` and ``set_volume`` stay Mac tools.
_NORMALIZED_TOOLS = {
    "device_command": "device_command",
    "portal_device_command": "device_command",
    "self_device_command": "device_command",
}

DEVICE_COMMAND_DECLARED_NAMES = ("device_command",)

_APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_ARGS_BYTES = 1024

_DESCRIPTION = (
    "Send one named system command to the connected Portal. "
    "The Portal owns its volume, brightness, media transport, and opening an app. "
    "Do not call Mac set_volume, media_control, or open_app for those, and do not "
    "change desktop Spotify or the Mac output volume. "
    "name is one of: "
    + ", ".join(DEVICE_COMMAND_NAMES)
    + ". "
    "set_volume_percent and set_brightness_percent need args.percent, an integer from 0 to 100. "
    'A request for 50% is set_volume_percent with args {"percent": 50}. '
    "set_volume_level needs args.level, an integer from 0 to 100. "
    "open_app needs args.id, a short app id such as spotify. "
    "Every other name takes no args: omit args or pass {}. "
    "This sends one frame and does not wait for an ack. "
    "ok means the frame was sent. "
    "get_volume does not return a volume number; the Portal handles that on the device. "
    "This does not speak audio."
)

DEVICE_COMMAND_INSTRUCTIONS = (
    "Portal system controls stay on this device. Volume, brightness, media transport, "
    "and opening an app on the Portal are not Mac desktop actions. "
    "When the user asks for any of those, or to show settings, or to reconnect the bridge, "
    "call device_command. "
    "If the Portal already handled the change on the device, leave it there. "
    "Never call set_volume, media_control, open_app, or any other Mac tool for Portal volume, "
    "Portal brightness, Portal media, or an app on the Portal. "
    "Do not change desktop Spotify or the Mac output volume for those requests. "
    "A request that names the Mac, such as the Mac's own volume or an app on the Mac, is the "
    "Mac tool. "
    "name is one of: "
    + ", ".join(DEVICE_COMMAND_NAMES)
    + ". "
    'A request such as 50% is set_volume_percent with args {"percent": 50}. '
    "set_brightness_percent uses args.percent the same way, an integer from 0 to 100. "
    "set_volume_level uses args.level, an integer from 0 to 100. "
    "open_app uses args.id, a short app id such as spotify. "
    "The other names take no args. "
    "ok means the frame was sent. The tool does not wait for an ack. "
    "get_volume returns reading on_device and no volume number; do not invent one. "
    "Do not claim the Portal changed before the tool returns ok."
)


def device_commands_enabled() -> bool:
    """Grok voice declares the Portal device command unless this is explicitly off."""
    raw = os.getenv("STACKCHAN_DEVICE_COMMANDS")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_device_command_tool_name(name: str) -> str:
    lowered = name.strip().lower().replace(".", "_")
    collapsed = re.sub(r"[^a-z0-9_]+", "_", lowered)
    return re.sub(r"_+", "_", collapsed).strip("_")


def device_command_for_tool(name: object) -> str | None:
    """Return ``device_command`` when ``name`` is this tool, else None."""
    if not isinstance(name, str) or not name.strip():
        return None
    if _NORMALIZED_TOOLS.get(normalize_device_command_tool_name(name)) is None:
        return None
    return "device_command"


def _int_in_range(value: object) -> bool:
    return type(value) is int and 0 <= value <= 100


def _coerce_args(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text.encode("utf-8")) > _MAX_ARGS_BYTES:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > _MAX_ARGS_BYTES:
        return None
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        return None
    return parsed


def _validate_command_args(command: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if command in _NO_ARG_COMMANDS:
        if args:
            return None
        return {}
    if command in _PERCENT_COMMANDS:
        if set(args) != {"percent"} or not _int_in_range(args.get("percent")):
            return None
        return {"percent": args["percent"]}
    if command in _LEVEL_COMMANDS:
        if set(args) != {"level"} or not _int_in_range(args.get("level")):
            return None
        return {"level": args["level"]}
    if command in _APP_COMMANDS:
        app_id = args.get("id")
        if set(args) != {"id"} or not isinstance(app_id, str) or _APP_ID_RE.fullmatch(app_id) is None:
            return None
        return {"id": app_id}
    return None


def canonical_command_name(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    return _CANONICAL_NAMES.get(name.strip().lower())


def build_device_command_frame(tool_name: object, arguments: object) -> dict[str, Any] | None:
    """Return the device frame, or None when the call cannot be sent."""
    if device_command_for_tool(tool_name) is None or not isinstance(arguments, dict):
        return None
    if set(arguments) - {"name", "args"}:
        return None
    command = canonical_command_name(arguments.get("name"))
    if command is None:
        return None
    parsed = _coerce_args(arguments.get("args", {}))
    if parsed is None:
        return None
    args = _validate_command_args(command, parsed)
    if args is None:
        return None
    return {"type": "device_command", "name": command, "args": args}


def is_device_command_frame(frame: object) -> bool:
    if not isinstance(frame, dict) or frame.get("type") != "device_command":
        return False
    if set(frame) != {"type", "name", "args"}:
        return False
    command = frame.get("name")
    if command not in DEVICE_COMMAND_NAMES or not isinstance(frame.get("args"), dict):
        return False
    cleaned = _validate_command_args(command, frame["args"])
    return cleaned is not None and cleaned == frame["args"]


def device_command_parameters(name: str) -> dict[str, Any]:
    if device_command_for_tool(name) is None:
        raise ValueError("not a device command tool")
    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": list(DEVICE_COMMAND_NAMES),
                "description": "Catalog v1 Portal command name.",
            },
            "args": {
                "type": "object",
                "description": (
                    "Command arguments. set_volume_percent and set_brightness_percent use "
                    "percent (integer 0..100). set_volume_level uses level (integer 0..100). "
                    "open_app uses id (short app id). Other commands omit args."
                ),
                "properties": {
                    "percent": {"type": "integer", "minimum": 0, "maximum": 100},
                    "level": {"type": "integer", "minimum": 0, "maximum": 100},
                    "id": {"type": "string"},
                },
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }


def device_command_description(name: str) -> str:
    if device_command_for_tool(name) is None:
        raise ValueError("not a device command tool")
    if name != "device_command":
        return "Alias of device_command. " + _DESCRIPTION
    return _DESCRIPTION


def device_command_declarations() -> list[dict[str, Any]]:
    return [
        {
            "name": declared,
            "description": device_command_description(declared),
            "parameters": device_command_parameters(declared),
        }
        for declared in DEVICE_COMMAND_DECLARED_NAMES
    ]


def device_command_extension(esp32: Any) -> Any:
    """Voice-tool extension that validates a command and sends one frame.

    Ok means the frame was written. Portal does not ack ``type:device_command``.
    ``get_volume`` does not wait for ``device_command_result``.
    """
    from .voice_tools import ToolExtension, ToolMetadata

    declarations = tuple(device_command_declarations())

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        frame = build_device_command_frame(name, args)
        if frame is None:
            return {"ok": False, "error": "invalid device command arguments"}
        send = getattr(esp32, "send_device_command", None)
        if not callable(send):
            return {"ok": False, "error": "device command channel unavailable"}
        try:
            result, error = await send(frame)
        except Exception:
            return {"ok": False, "error": "device command frame failed"}
        if error:
            message = "device command frame failed"
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
            return {"ok": False, "error": message}
        if isinstance(result, dict) and result.get("ok") is True:
            payload: dict[str, Any] = {"ok": True, "name": frame["name"]}
            if frame["name"] == "get_volume":
                payload["reading"] = "on_device"
            return payload
        return {"ok": False, "error": "device command frame rejected"}

    return ToolExtension(
        declarations=declarations,
        dispatch=dispatch,
        metadata={
            name: ToolMetadata(True, "portal_device", 5.0) for name in DEVICE_COMMAND_DECLARED_NAMES
        },
        instructions=DEVICE_COMMAND_INSTRUCTIONS,
    )
