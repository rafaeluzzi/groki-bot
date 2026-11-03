# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Thin XiaoZhi ``type:panel`` frames for the Portal screen.

The Grok login-voice path declares these tools locally and forwards a call as
one JSON text frame on the device WebSocket. The frame does not use MCP, so
it still works while hello advertises ``features.mcp: false``.

Tool names (any of these, case-insensitive; dots become underscores):

| Call name | Action | Frame |
|---|---|---|
| ``show_panel``, ``panel_show``, ``self.panel.show`` | show | ``{"type":"panel","panel":{...}}`` |
| ``dismiss_panel``, ``panel_dismiss``, ``self.panel.dismiss`` | dismiss | ``{"type":"panel","dismiss":true}`` |

``show_panel`` takes the panel document as a JSON object or as a JSON string
of that object. Version 1 kinds ``list``, ``now_playing``, and ``choice`` are
checked before the frame is sent. A ``webview`` or ``html`` document is
forwarded the same way when it carries the page text, so a later device
renderer can show it without a new tool argument. Extra fields on a valid
document are kept.

Portal does not ack ``type:panel`` today (unlike ``type:core``). The tool
returns ok after the document is validated and the frame is written. That
write does not send ``abort`` and does not change the voice session. Inbound
``type:panel`` text is not user speech and is not forwarded into the voice
turn. Device ``panel_action`` delivery to the model is not implemented.

Set ``STACKCHAN_PANEL_FRAMES=0`` to stop declaring the tools on the gbot path.
The default is on, same as ``STACKCHAN_CORE_FRAMES``.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any

PANEL_KINDS = ("list", "now_playing", "choice")
PASSTHROUGH_KINDS = ("webview", "html")
_ALL_KINDS = PANEL_KINDS + PASSTHROUGH_KINDS

# Normalized spellings the voice model or a session catalog might use.
_NORMALIZED_ACTIONS = {
    "show_panel": "show",
    "panel_show": "show",
    "self_panel_show": "show",
    "dismiss_panel": "dismiss",
    "panel_dismiss": "dismiss",
    "self_panel_dismiss": "dismiss",
}

# Declared on the gbot session. Dotted names are sanitized to underscores on
# the provider wire (``self.panel.show`` -> ``self_panel_show``) and dispatched
# back to the original name. Do not also declare the sanitized spelling: that
# collides with the dotted form.
PANEL_DECLARED_NAMES = (
    "show_panel",
    "self.panel.show",
    "dismiss_panel",
    "self.panel.dismiss",
)

_CANONICAL = {
    "show": "show_panel",
    "dismiss": "dismiss_panel",
}

MAX_PANEL_BYTES = 32768
_MAX_DEPTH = 8
_MAX_ITEMS = 40
_MAX_OPTIONS = 12
_MAX_KEYS = 48
_MAX_LABEL = 240
_LABEL_KEYS = ("title", "label", "text", "name")
_NOW_PLAYING_KEYS = ("title", "track", "name", "song")
_CHOICE_PROMPT_KEYS = ("prompt", "title", "text")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

_SHOW_DESCRIPTION = (
    "Show a panel beside the orb on the connected Portal. "
    "panel is a version 1 document, as an object or a JSON string. "
    "kind list needs items with a title, label, or text. "
    "kind now_playing needs a title, track, or name. "
    "kind choice needs a prompt or title and options with a label. "
    "kind webview or html is forwarded when it includes the page text, "
    "for example html, document, url, or src. "
    "Use list, now_playing, or choice when they fit. "
    "This does not speak audio and does not replace the spoken reply. "
    "ok means the frame was accepted and sent."
)

_DISMISS_DESCRIPTION = (
    "Close the panel on the connected Portal screen. "
    "This does not speak audio. ok means the dismiss frame was sent."
)

PANEL_INSTRUCTIONS = (
    "A panel sits beside the orb and takes about two thirds of the screen. "
    "When a list, what is playing, or a short choice fits, call show_panel with a "
    "version 1 panel document of kind list, now_playing, or choice. "
    "self.panel.show is the same action. "
    "dismiss_panel and self.panel.dismiss close it. "
    "A webview or html document can be sent the same way when that page is what should appear. "
    "Panels do not replace your spoken reply. "
    "Do not claim the panel showed before the tool returns ok. "
    "Do not end every reply with an open-ended follow-up."
)


def panel_frames_enabled() -> bool:
    """Grok voice declares Portal panel tools unless this is explicitly off."""
    raw = os.getenv("STACKCHAN_PANEL_FRAMES")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_panel_tool_name(name: str) -> str:
    lowered = name.strip().lower().replace(".", "_")
    collapsed = re.sub(r"[^a-z0-9_]+", "_", lowered)
    return re.sub(r"_+", "_", collapsed).strip("_")


def panel_action_for_tool(name: object) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return _NORMALIZED_ACTIONS.get(normalize_panel_tool_name(name))


def canonical_panel_tool(name: object) -> str | None:
    action = panel_action_for_tool(name)
    if action is None:
        return None
    return _CANONICAL[action]


def _text(value: object, limit: int) -> bool:
    if not isinstance(value, str) or len(value) > limit:
        return False
    if not value.strip() or len(value.strip()) > limit:
        return False
    return not any(ord(ch) < 32 and ch not in "\n\t\r" for ch in value)


def _identifier(value: object) -> bool:
    if type(value) is int:
        return 0 <= value <= 1_000_000
    return _text(value, 64)


def _key_ok(key: object) -> bool:
    return isinstance(key, str) and _KEY_RE.fullmatch(key) is not None


def _walk(value: object, depth: int) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return "\x00" not in value and not any(ord(ch) < 32 and ch not in "\n\t\r" for ch in value)
    if isinstance(value, list):
        return len(value) <= _MAX_ITEMS and all(_walk(item, depth + 1) for item in value)
    if isinstance(value, dict):
        if len(value) > _MAX_KEYS:
            return False
        return all(_key_ok(key) and _walk(item, depth + 1) for key, item in value.items())
    return False


def _labeled(item: object, keys: tuple[str, ...]) -> bool:
    if not isinstance(item, dict):
        return False
    present = [key for key in keys if key in item]
    if not present:
        return False
    return all(_text(item[key], _MAX_LABEL) for key in present)


def _option_list(value: object, *, limit: int) -> bool:
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        return False
    for option in value:
        if not _labeled(option, ("label", "title", "text")):
            return False
        if "id" in option and not _identifier(option["id"]):
            return False
    return True


def _accept_list(document: dict[str, Any]) -> bool:
    if "title" in document and not _text(document["title"], _MAX_LABEL):
        return False
    items = document.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= _MAX_ITEMS:
        return False
    for item in items:
        if not _labeled(item, _LABEL_KEYS):
            return False
        if "id" in item and not _identifier(item["id"]):
            return False
        if "subtitle" in item and not _text(item["subtitle"], _MAX_LABEL):
            return False
    return True


def _accept_now_playing(document: dict[str, Any]) -> bool:
    present = [key for key in _NOW_PLAYING_KEYS if key in document]
    if not present or any(not _text(document[key], _MAX_LABEL) for key in present):
        return False
    for key in ("artist", "album", "subtitle"):
        if key in document and not _text(document[key], _MAX_LABEL):
            return False
    if "progress" in document:
        progress = document["progress"]
        if isinstance(progress, bool) or not isinstance(progress, (int, float)):
            return False
        if not 0 <= float(progress) <= 1:
            return False
    for key in ("position_ms", "duration_ms"):
        if key not in document:
            continue
        value = document[key]
        if type(value) is not int or value < 0 or value > 24 * 60 * 60 * 1000:
            return False
    if "paused" in document and not isinstance(document["paused"], bool):
        return False
    return True


def _accept_choice(document: dict[str, Any]) -> bool:
    prompts = [key for key in _CHOICE_PROMPT_KEYS if key in document]
    if not prompts or any(not _text(document[key], _MAX_LABEL) for key in prompts):
        return False
    lists = [key for key in ("options", "choices") if key in document]
    if not lists:
        return False
    return all(_option_list(document[key], limit=_MAX_OPTIONS) for key in lists)


def _has_page_text(value: object, *, key: str | None = None) -> bool:
    """True when a webview or html document carries some page text.

    The field name is not fixed, so a later renderer can use html, url, or
    another string without a new tool argument. ``kind`` itself does not count.
    """
    if isinstance(value, str):
        return key not in {None, "kind"} and bool(value.strip())
    if isinstance(value, dict):
        return any(_has_page_text(item, key=name) for name, item in value.items())
    if isinstance(value, list):
        return any(_has_page_text(item) for item in value)
    return False


def _accept_passthrough(document: dict[str, Any]) -> bool:
    return _has_page_text(document)


_KIND_CHECKS = {
    "list": _accept_list,
    "now_playing": _accept_now_playing,
    "choice": _accept_choice,
    "webview": _accept_passthrough,
    "html": _accept_passthrough,
}


def _coerce_document(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text.encode("utf-8")) > MAX_PANEL_BYTES:
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
    if len(encoded.encode("utf-8")) > MAX_PANEL_BYTES:
        return None
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        return None
    return parsed


def validate_panel_document(value: object) -> dict[str, Any] | None:
    """Return a JSON copy of a version-1 panel document, or None."""
    document = _coerce_document(value)
    if document is None or not _walk(document, 1):
        return None
    if type(document.get("version")) is not int or document.get("version") != 1:
        return None
    kind = document.get("kind")
    if kind not in _ALL_KINDS:
        return None
    if not _KIND_CHECKS[kind](document):
        return None
    return document


def build_panel_frame(tool_name: object, arguments: object) -> dict[str, Any] | None:
    """Return the device frame, or None when the call cannot be sent."""
    action = panel_action_for_tool(tool_name)
    if action is None or not isinstance(arguments, dict):
        return None
    if action == "dismiss":
        if arguments:
            return None
        return {"type": "panel", "dismiss": True}
    if set(arguments) != {"panel"}:
        return None
    document = validate_panel_document(arguments.get("panel"))
    if document is None:
        return None
    return {"type": "panel", "panel": document}


def is_panel_frame(frame: object) -> bool:
    if not isinstance(frame, dict) or frame.get("type") != "panel":
        return False
    if set(frame) == {"type", "dismiss"}:
        return frame.get("dismiss") is True
    if set(frame) != {"type", "panel"}:
        return False
    document = validate_panel_document(frame.get("panel"))
    return document is not None and document == frame.get("panel")


def panel_tool_parameters(name: str) -> dict[str, Any]:
    action = panel_action_for_tool(name)
    if action is None:
        raise ValueError("not a panel tool")
    if action == "dismiss":
        return {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "panel": {
                "description": _SHOW_DESCRIPTION,
                "anyOf": [{"type": "object"}, {"type": "string"}],
            },
        },
        "required": ["panel"],
        "additionalProperties": False,
    }


def panel_tool_description(name: str) -> str:
    action = panel_action_for_tool(name)
    if action is None:
        raise ValueError("not a panel tool")
    canonical = _CANONICAL[action]
    text = _DISMISS_DESCRIPTION if action == "dismiss" else _SHOW_DESCRIPTION
    if name != canonical:
        return f"Alias of {canonical}. " + text
    return text


def panel_tool_declarations() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": panel_tool_description(name),
            "parameters": panel_tool_parameters(name),
        }
        for name in PANEL_DECLARED_NAMES
    ]


def panel_frame_extension(esp32: Any) -> Any:
    """Voice-tool extension that validates a panel and sends one frame.

    Ok means the frame was written. Portal does not ack ``type:panel``.
    """
    from .voice_tools import ToolExtension, ToolMetadata

    declarations = tuple(panel_tool_declarations())

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        frame = build_panel_frame(name, args)
        if frame is None:
            return {"ok": False, "error": "invalid panel arguments"}
        send = getattr(esp32, "send_panel", None)
        if not callable(send):
            return {"ok": False, "error": "panel channel unavailable"}
        try:
            result, error = await send(frame)
        except Exception:
            return {"ok": False, "error": "panel frame failed"}
        if error:
            message = "panel frame failed"
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
            return {"ok": False, "error": message}
        if isinstance(result, dict) and result.get("ok") is True:
            if frame.get("dismiss") is True:
                return {"ok": True, "dismiss": True}
            return {"ok": True, "kind": frame["panel"]["kind"]}
        return {"ok": False, "error": "panel frame rejected"}

    return ToolExtension(
        declarations=declarations,
        dispatch=dispatch,
        metadata={
            name: ToolMetadata(True, "portal_panel", 5.0) for name in PANEL_DECLARED_NAMES
        },
        instructions=PANEL_INSTRUCTIONS,
    )
