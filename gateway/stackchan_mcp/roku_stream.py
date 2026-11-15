# SPDX-License-Identifier: MIT
"""Put a browser video on the living-room Roku.

The login-voice path declares ``roku_stream`` and ``roku_stream_stop``.
A call POSTs ``/api/roku/stream`` or ``/api/roku/stream/stop`` on the router
(``STACKCHAN_ROUTER_URL``, default ``http://127.0.0.1:8080``). The router owns
yt-dlp, the stream proxy, and ECP. This tool does not call the Roku and it
does not start anything on the Mac.

A direct file launches before this returns. A page comes back at once with
"Looking for the stream on <site>…"; the router finishes yt-dlp in the
background. Both calls wait about 8 seconds, the same as ``roku_play``.
The voice executor's own limit sits a little above that.

When ``STACKCHAN_TOKEN`` is set, the posts send it as a bearer token. The
router already requires that secret on these two routes.

Set ``STACKCHAN_ROKU_STREAM=0`` to stop declaring both tools.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

ROKU_STREAM_TOOL = "roku_stream"
ROKU_STREAM_STOP_TOOL = "roku_stream_stop"
STREAM_POST_TIMEOUT_S = 8
STOP_POST_TIMEOUT_S = 8

_DESCRIPTION = (
    "Put the video in Uzzi's browser, or a link he names, on the living-room TV "
    "through the Koji channel. source is chrome for the front Chrome tab, or url "
    "with the link. title is optional. Say spoken_summary out loud. "
    "A page comes back at once, for example Looking for the stream on the site. "
    "If ok is false, say the reason. This is not for a movie or series on Netflix, "
    "Max, or his other TV apps; that is roku_play. Do not play it on the Mac."
)

_STOP_DESCRIPTION = (
    "Stop the browser video on the living-room TV and leave the Koji channel. "
    "Say spoken_summary out loud. If ok is false, say the reason."
)

ROKU_STREAM_INSTRUCTIONS = (
    "What's in the browser, or a link, on the living-room TV is roku_stream. "
    "source chrome is the front Chrome tab. source url is a link he gave, and url is required then. "
    "Say spoken_summary out loud as you call it. A direct link is Putting the game on the TV. "
    "A page is Looking for the stream on the site. "
    "If ok is false, say the reason in the result. Do not explain how the stream works. "
    "A movie or series on Netflix, Prime, Hulu, Disney, Max, Paramount, Apple TV, or YouTube "
    "is roku_play, not roku_stream. "
    "roku_stream_stop takes it off the TV. "
    "Do not play the video on the Mac. This does not power the projector."
)


def roku_stream_enabled() -> bool:
    raw = os.getenv("STACKCHAN_ROKU_STREAM")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def router_origin() -> str:
    raw = os.getenv("STACKCHAN_ROUTER_URL", "http://127.0.0.1:8080").strip()
    return raw.rstrip("/") or "http://127.0.0.1:8080"


def router_headers() -> dict[str, str]:
    token = (os.getenv("STACKCHAN_TOKEN") or os.getenv("BEARER_TOKEN") or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def stream_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "enum": ["chrome", "url"],
                "description": "chrome is the front Chrome tab. url is a link he named.",
            },
            "url": {"type": "string", "description": "The link, when source is url."},
            "title": {"type": "string", "description": "Optional short title for the spoken line."},
        },
        "required": ["source"],
        "additionalProperties": False,
    }


def stop_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def stream_payload(args: dict[str, Any]) -> dict[str, Any] | None:
    source = args.get("source")
    if source == "chrome":
        payload: dict[str, Any] = {"source": "chrome"}
        title = args.get("title")
        if isinstance(title, str) and title.strip():
            payload["title"] = title.strip()
        return payload
    if source != "url":
        return None
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    payload = {"source": "url", "url": url.strip()}
    title = args.get("title")
    if isinstance(title, str) and title.strip():
        payload["title"] = title.strip()
    return payload


def _down() -> dict[str, Any]:
    return {
        "ok": False,
        "spoken_summary": "I can't reach the TV.",
        "error": "roku router failed",
    }


async def _post(path: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{router_origin()}{path}", json=payload, headers=router_headers()
            ) as response:
                body = await response.json(content_type=None)
                status = response.status
    except Exception:
        logger.warning("roku stream request failed")
        return _down()
    if isinstance(body, dict) and isinstance(body.get("spoken_summary"), str) and body["spoken_summary"]:
        return body
    if status >= 400 or not isinstance(body, dict):
        return _down()
    return body


async def post_stream(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("/api/roku/stream", payload, STREAM_POST_TIMEOUT_S)


async def post_stop() -> dict[str, Any]:
    return await _post("/api/roku/stream/stop", {}, STOP_POST_TIMEOUT_S)


def roku_stream_extension() -> Any:
    """Voice-tool extension. The router launches; this only posts the request."""
    from .voice_tools import ToolExtension, ToolMetadata

    declarations = (
        {
            "name": ROKU_STREAM_TOOL,
            "description": _DESCRIPTION,
            "parameters": stream_parameters(),
        },
        {
            "name": ROKU_STREAM_STOP_TOOL,
            "description": _STOP_DESCRIPTION,
            "parameters": stop_parameters(),
        },
    )

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == ROKU_STREAM_STOP_TOOL:
            return await post_stop()
        if name != ROKU_STREAM_TOOL:
            return {"ok": False, "error": "unknown roku tool"}
        payload_args = args if isinstance(args, dict) else {}
        if payload_args.get("source") not in {"chrome", "url"}:
            return {"ok": False, "error": "source is required"}
        payload = stream_payload(payload_args)
        if payload is None:
            return {"ok": False, "error": "url is required"}
        return await post_stream(payload)

    return ToolExtension(
        declarations=declarations,
        dispatch=dispatch,
        metadata={
            ROKU_STREAM_TOOL: ToolMetadata(True, "roku_stream", 12.0),
            ROKU_STREAM_STOP_TOOL: ToolMetadata(True, "roku_stream", 12.0),
        },
        instructions=ROKU_STREAM_INSTRUCTIONS,
    )
