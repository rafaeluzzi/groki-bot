# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Thin XiaoZhi ``type:core`` frames for the Portal screen.

The Grok login-voice path declares these tools locally and forwards a call as
one JSON text frame on the device WebSocket. Portal answers
``{"type":"core","ok":true,"action":"..."}``. The frame does not use MCP, so
it still works while hello advertises ``features.mcp: false``.

Tool names (any of these, case-insensitive; dots become underscores):

| Call name | Action | Frame |
|---|---|---|
| ``core_morph``, ``morph``, ``self.core.morph`` | morph | ``{"type":"core","action":"morph","name":"..."}`` |
| ``core_say``, ``say``, ``self.core.say`` | say | ``{"type":"core","action":"say","text":"..."}`` |

Portal expression on this path is morph and say only. ``core_draw``, ``draw``,
``self.core.draw``, and dotted twins such as ``core.draw`` are not tools and
do not build a frame.

Argument keys ``face`` / ``emotion`` fill ``name`` when ``name`` is empty.
``message`` fills ``text``. The frame itself only carries the canonical field.

MCP gap (why Portal cannot set ADVERTISE_MCP from this change alone):

The gateway already sends MCP ``initialize`` then ``tools/list`` when hello
sets ``features.mcp`` true (``ESP32Manager._init_device``). It skips both
while Portal sends ``features.mcp: false``, because a voice-only peer never
answers and the init task would wait out the MCP timeout. Remaining work
before advertising MCP is useful:

1. Portal must answer that handshake. Initialize params are
   ``{"capabilities": {...}}`` (optional ``vision``), the XiaoZhi
   device-server dialect. This repo does not send ``notifications/initialized``
   and does not send a client-style ``protocolVersion`` / ``clientInfo``.
2. ``tools/list`` runs after the Grok ``session.update``, in a background
   task. Discovered names are stored for status and external relays. They are
   not copied into the live voice tool list, and a later ``tools/call`` is not
   rewritten into a ``type:core`` frame.
3. Until those two steps exist, keep hello at ``features.mcp: false`` and use
   the thin frames below. Set ``STACKCHAN_CORE_FRAMES=0`` to stop declaring
   them on the gbot path.
"""

from __future__ import annotations

import os
import re
from typing import Any

CORE_ACTIONS = ("morph", "say")

_ACTION_FIELDS = {
    "morph": "name",
    "say": "text",
}

_ARG_FALLBACKS = {
    "morph": ("name", "face", "emotion"),
    "say": ("text", "message"),
}

# Normalized spellings the voice model or a session catalog might use.
# Draw names are absent on purpose: a leftover alias must not send a frame.
_NORMALIZED_ACTIONS = {
    "morph": "morph",
    "say": "say",
    "core_morph": "morph",
    "core_say": "say",
    "self_core_morph": "morph",
    "self_core_say": "say",
}

# Declared on the gbot session. Dotted names are sanitized to underscores on
# the provider wire (``self.core.morph`` -> ``self_core_morph``) and dispatched
# back to the original name. Do not also declare the sanitized spelling: that
# collides with the dotted form.
CORE_DECLARED_NAMES = (
    "core_morph",
    "morph",
    "self.core.morph",
    "core_say",
    "say",
    "self.core.say",
)

_MAX_CHARS = 500

_DESCRIPTIONS = {
    "morph": (
        "Change the face on the connected Portal screen. "
        "name is a short face id such as heart. This does not speak audio."
    ),
    "say": (
        "Show a short line of text on the connected Portal screen as speech text. "
        "text is the line to show. This does not play audio."
    ),
}

CORE_INSTRUCTIONS = (
    "You are Koji and you have a fluid SDF body on this Portal; modes and morphs are your face. "
    "When asked what you are looking at or what is on the screen, answer in spoken English: "
    "Idle is a calm orb. Listening, Thinking, and Speaking are mode motion of the same body. "
    "Morph makes the silhouette a named shape like a heart, then it melts back. "
    "Say is inner glow words. "
    "Effects stay inside the body silhouette. Do not claim the camera or a photo is on screen. "
    "Portal screen expression is morph and say only. When the user asks to change the face "
    "or show words on the screen, call core_morph(name) or core_say(text). "
    "morph, say, self.core.morph, and self.core.say are the same two actions. "
    "These update the screen and do not replace your "
    "spoken reply. Do not claim the screen changed before the tool returns ok."
)


def core_frames_enabled() -> bool:
    """Grok voice declares Portal core tools unless this is explicitly off."""
    raw = os.getenv("STACKCHAN_CORE_FRAMES")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def core_ack_timeout_s() -> float:
    raw = os.getenv("STACKCHAN_CORE_ACK_TIMEOUT_S", "3")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 3.0
    if not 0 < value <= 30:
        return 3.0
    return value


def normalize_core_tool_name(name: str) -> str:
    lowered = name.strip().lower().replace(".", "_")
    collapsed = re.sub(r"[^a-z0-9_]+", "_", lowered)
    return re.sub(r"_+", "_", collapsed).strip("_")


def core_action_for_tool(name: object) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return _NORMALIZED_ACTIONS.get(normalize_core_tool_name(name))


def canonical_core_tool(name: object) -> str | None:
    action = core_action_for_tool(name)
    if action is None:
        return None
    return "core_" + action


def _payload_text(action: str, arguments: dict[str, Any]) -> str | None:
    for key in _ARG_FALLBACKS[action]:
        raw = arguments.get(key)
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text or len(text) > _MAX_CHARS:
            continue
        if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
            continue
        return text
    return None


def build_core_frame(tool_name: object, arguments: object) -> dict[str, Any] | None:
    """Return the thin device frame, or None when the call cannot be sent."""
    action = core_action_for_tool(tool_name)
    if action is None or not isinstance(arguments, dict):
        return None
    text = _payload_text(action, arguments)
    if text is None:
        return None
    return {"type": "core", "action": action, _ACTION_FIELDS[action]: text}


def is_thin_core_frame(frame: object) -> bool:
    if not isinstance(frame, dict):
        return False
    action = frame.get("action")
    if action not in _ACTION_FIELDS or frame.get("type") != "core":
        return False
    field = _ACTION_FIELDS[action]
    if set(frame) != {"type", "action", field}:
        return False
    text = frame[field]
    return (
        isinstance(text, str)
        and bool(text.strip())
        and text == text.strip()
        and len(text) <= _MAX_CHARS
        and "\x00" not in text
    )


def core_tool_parameters(name: str) -> dict[str, Any]:
    action = core_action_for_tool(name)
    if action is None:
        raise ValueError("not a core tool")
    field = _ACTION_FIELDS[action]
    return {
        "type": "object",
        "properties": {
            field: {"type": "string", "description": _DESCRIPTIONS[action]},
        },
        "required": [field],
    }


def core_tool_description(name: str) -> str:
    action = core_action_for_tool(name)
    if action is None:
        raise ValueError("not a core tool")
    canonical = "core_" + action
    text = _DESCRIPTIONS[action]
    if name != canonical:
        return f"Alias of {canonical}. " + text
    return text


def core_tool_declarations() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": core_tool_description(name),
            "parameters": core_tool_parameters(name),
        }
        for name in CORE_DECLARED_NAMES
    ]


def core_frame_extension(esp32: Any) -> Any:
    """Voice-tool extension that sends one thin frame and waits for the ack."""
    from .voice_tools import ToolExtension, ToolMetadata

    declarations = tuple(core_tool_declarations())

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        frame = build_core_frame(name, args)
        if frame is None:
            return {"ok": False, "error": "invalid core arguments"}
        send = getattr(esp32, "send_core", None)
        if not callable(send):
            return {"ok": False, "error": "core channel unavailable"}
        try:
            ack, error = await send(frame)
        except Exception:
            return {"ok": False, "error": "core frame failed", "action": frame["action"]}
        action = frame["action"]
        if error:
            message = "core frame failed"
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
            return {"ok": False, "error": message, "action": action}
        if (
            isinstance(ack, dict)
            and ack.get("type") == "core"
            and ack.get("ok") is True
            and ack.get("action") == action
        ):
            return {"ok": True, "action": action}
        return {"ok": False, "error": "core frame rejected", "action": action}

    return ToolExtension(
        declarations=declarations,
        dispatch=dispatch,
        metadata={
            name: ToolMetadata(True, "portal_core", 8.0) for name in CORE_DECLARED_NAMES
        },
        instructions=CORE_INSTRUCTIONS,
    )
