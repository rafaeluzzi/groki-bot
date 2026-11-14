# SPDX-License-Identifier: MIT
"""Play a spoken title on the living-room Roku.

The login-voice path declares ``roku_play``. A call POSTs ``/api/roku/play``
on the router (``STACKCHAN_ROUTER_URL``, default ``http://127.0.0.1:8080``).
The router is on the iMac LAN and owns discovery, resolving, and ECP. This
tool does not call the Roku, and it does not start anything on the Mac.

Set ``STACKCHAN_ROKU_PLAY=0`` to stop declaring the tool.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

ROKU_TOOL_NAME = "roku_play"

_DESCRIPTION = (
    "Play a movie or series on the living-room Roku. "
    "title is the spoken title. service is optional (Netflix, Prime, Hulu, Disney, Max, "
    "Paramount, Apple TV, YouTube). type is movie or series when they said which. "
    "year is the four-digit year when they said one. "
    "Say spoken_summary out loud as you call this. "
    "If needs_confirm is ambiguous, ask which one and name the two options. "
    "If needs_confirm is rent, ask before spending money. Never rent or buy without a yes. "
    "Do not play the title on the Mac."
)

ROKU_INSTRUCTIONS = (
    "A movie or series on the living-room TV is roku_play. "
    "Say the spoken_summary out loud as you call it, for example Playing Dune: Part Two on Max. "
    "A wrong match that plays with no announcement is the failure to avoid. "
    "If needs_confirm is ambiguous, ask which one and name the two options. Do not play until they answer. "
    "If needs_confirm is rent, ask before any purchase. Never rent or buy without a yes. "
    "If the tool cannot reach the router, say the TV lookup is down. "
    "Do not open the title on the Mac, and do not use desktop media controls. "
    "This does not power the projector."
)


def roku_play_enabled() -> bool:
    raw = os.getenv("STACKCHAN_ROKU_PLAY")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def router_origin() -> str:
    raw = os.getenv("STACKCHAN_ROUTER_URL", "http://127.0.0.1:8080").strip()
    return raw.rstrip("/") or "http://127.0.0.1:8080"


def roku_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Spoken title to play."},
            "service": {
                "type": "string",
                "description": "Optional service, such as Netflix or Max.",
            },
            "type": {"type": "string", "enum": ["movie", "series"]},
            "year": {"type": "integer", "description": "Four-digit year, when they said one."},
        },
        "required": ["title"],
        "additionalProperties": False,
    }


def play_payload(args: dict[str, Any]) -> dict[str, Any] | None:
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    payload: dict[str, Any] = {"title": title.strip()}
    service = args.get("service")
    if isinstance(service, str) and service.strip():
        payload["service"] = service.strip()
    kind = args.get("type")
    if kind is not None:
        if kind not in {"movie", "series"}:
            return None
        payload["type"] = kind
    year = args.get("year")
    if year is not None:
        if type(year) is not int or year < 1900 or year > 2100:
            return None
        payload["year"] = year
    return payload


async def post_play(payload: dict[str, Any]) -> dict[str, Any]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{router_origin()}/api/roku/play", json=payload) as response:
                body = await response.json(content_type=None)
                status = response.status
    except Exception:
        logger.warning("roku play request failed")
        return {
            "ok": False,
            "spoken_summary": "I can't reach the TV lookup.",
            "error": "roku router failed",
        }
    if status >= 400 or not isinstance(body, dict):
        return {
            "ok": False,
            "spoken_summary": "I can't reach the TV lookup.",
            "error": "roku router failed",
        }
    return body


def roku_play_extension() -> Any:
    """Voice-tool extension. The router launches; this only posts the request."""
    from .voice_tools import ToolExtension, ToolMetadata

    declarations = (
        {
            "name": ROKU_TOOL_NAME,
            "description": _DESCRIPTION,
            "parameters": roku_parameters(),
        },
    )

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name != ROKU_TOOL_NAME:
            return {"ok": False, "error": "unknown roku tool"}
        payload = play_payload(args if isinstance(args, dict) else {})
        if payload is None:
            return {"ok": False, "error": "title is required"}
        return await post_play(payload)

    return ToolExtension(
        declarations=declarations,
        dispatch=dispatch,
        metadata={ROKU_TOOL_NAME: ToolMetadata(True, "roku_play", 12.0)},
        instructions=ROKU_INSTRUCTIONS,
    )
