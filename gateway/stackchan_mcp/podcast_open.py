# SPDX-License-Identifier: MIT
"""Open a followed show on Portal Spotify.

The login-voice path declares ``podcast_open_spotify``. A call reads
``GET /api/podcasts/followed`` (A-to-Z by title, the same order as the
"Podcasts I follow" panel), resolves a title or ``number N``, then
``POST /api/podcasts/open`` and writes one ``type:podcast`` frame.
The Portal speaks the announce line and opens Spotify. This does not
start a player on the Mac.

Set ``STACKCHAN_PODCAST_OPEN=0`` to stop declaring the tool.
``STACKCHAN_ROUTER_URL`` is the router origin (default ``http://127.0.0.1:8080``).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

PODCAST_TOOL_NAME = "podcast_open_spotify"

_DESCRIPTION = (
    "Open a followed podcast in the Spotify app on the Portal. "
    "show is the show title, or number N. "
    "N counts from the top of the Podcasts I follow panel. "
    "That panel and the followed list are both A-to-Z by title, so number 1 is the first title in that order. "
    "Pass the words the user said when the title is unclear, including a misheard name. "
    "On a miss the result lists the followed shows in that same order; call again with the title they meant. "
    "A hit sends one type:podcast frame. The Portal says the announce line and opens Spotify. "
    "If the result says the Portal already said it, do not repeat it. "
    "Do not open Spotify on the Mac, and do not call Mac media_control or open_app for this."
)

PODCAST_INSTRUCTIONS = (
    "A podcast play, open, or what's-new that did not match on the Portal is yours. "
    "Call podcast_open_spotify with show set to the title or to number N. "
    "number N is the Nth show in the Podcasts I follow panel, which is A-to-Z by title, "
    "the same order as the followed list. "
    "When you show that panel, list the titles in that order from the top. "
    "Do not open Spotify on the Mac for a Portal podcast. "
    "If the tool says the Portal already said the announce line, do not repeat it."
)

_CARDINALS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
}
_NUMBER_WORDS = "|".join([*_CARDINALS, *_ORDINAL_WORDS])
_ORDINAL_RE = re.compile(rf"^(?:(?:number|no)\s+)?(\d+|{_NUMBER_WORDS})$")


def podcast_open_enabled() -> bool:
    raw = os.getenv("STACKCHAN_PODCAST_OPEN")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def router_origin() -> str:
    raw = os.getenv("STACKCHAN_ROUTER_URL", "http://127.0.0.1:8080").strip()
    return raw.rstrip("/") or "http://127.0.0.1:8080"


def followed_order(shows: list) -> list[dict]:
    """Same key as the router's ``followed()`` sort and the panel row sort."""
    rows = [show for show in shows if isinstance(show, dict)]
    return sorted(rows, key=lambda show: str(show.get("title") or "").lower())


def title_key(value: str) -> str:
    """Case and punctuation folded, matching the router's show query."""
    name = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
    name = " ".join(name.split())
    for suffix in (" podcasts", " podcast"):
        if name.endswith(suffix):
            trimmed = name[: -len(suffix)].strip()
            if trimmed:
                return trimmed
    return name


def match_followed(query: str, shows: list[dict]) -> list[dict]:
    needle = title_key(query)
    if not needle:
        return []

    def title_of(show: dict) -> str:
        return " ".join(
            "".join(ch.lower() if ch.isalnum() else " " for ch in str(show.get("title") or "")).split()
        )

    exact = [show for show in shows if title_of(show) == needle]
    if exact:
        return exact
    prefix = [show for show in shows if title_of(show).startswith(needle)]
    if prefix:
        return prefix
    return [show for show in shows if needle in title_of(show)]


def ordinal_index(query: str) -> int | None:
    """1-based index for 'number six' or '6'. None when the words are a title."""
    match = _ORDINAL_RE.match(title_key(query))
    if match is None:
        return None
    token = match.group(1)
    if token.isdigit():
        number = int(token)
    else:
        number = _CARDINALS.get(token) or _ORDINAL_WORDS.get(token) or 0
    if number < 1:
        return None
    return number


def catalog(shows: list[dict]) -> list[dict[str, Any]]:
    return [{"n": index, "title": str(show.get("title") or "")} for index, show in enumerate(shows, start=1)]


def plan_open(query: str, shows: list) -> dict[str, Any]:
    """Pick a followed show, or a miss the model can correct.

    Title matches win over an ordinal, so a show named like a number stays a title.
    ``number N`` uses [followed_order], which is the panel order.
    """
    ordered = followed_order(shows)
    listed = catalog(ordered)
    text = " ".join((query or "").split())
    if not text:
        return {"kind": "miss", "result": _miss("Which show?", listed)}
    matches = match_followed(text, ordered)
    if len(matches) == 1:
        return {"kind": "open", "title": str(matches[0].get("title") or text)}
    if len(matches) > 1:
        names = ", ".join(str(show.get("title") or "a show") for show in matches[:3])
        return {"kind": "ask", "result": _miss(f"Which show? {names}.", listed)}
    number = ordinal_index(text)
    if number is None:
        return {"kind": "miss", "result": _miss("No followed show matched.", listed)}
    if number > len(ordered):
        return {"kind": "range", "result": _miss(f"There is no show number {number}.", listed)}
    return {"kind": "open", "title": str(ordered[number - 1].get("title") or "")}


def podcast_frame(body: dict) -> dict[str, Any] | None:
    """The Portal frame for a resolved show. None when the open call did not pick one."""
    if not body.get("showId"):
        return None
    frame: dict[str, Any] = {
        "type": "podcast",
        "announce": str(body.get("announce") or ""),
    }
    for key in ("openSpotifyUri", "openSpotifyUrl"):
        value = body.get(key)
        if isinstance(value, str) and value:
            frame[key] = value
    return frame


def is_podcast_frame(frame: object) -> bool:
    if not isinstance(frame, dict) or frame.get("type") != "podcast":
        return False
    if not isinstance(frame.get("announce"), str):
        return False
    allowed = {"type", "announce", "openSpotifyUri", "openSpotifyUrl"}
    if any(key not in allowed for key in frame):
        return False
    for key in ("openSpotifyUri", "openSpotifyUrl"):
        value = frame.get(key)
        if value is not None and not isinstance(value, str):
            return False
    return True


def podcast_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "show": {
                "type": "string",
                "description": "Followed show title, or number N in the Podcasts I follow list.",
            },
        },
        "required": ["show"],
        "additionalProperties": False,
    }


def podcast_declarations() -> list[dict[str, Any]]:
    return [
        {
            "name": PODCAST_TOOL_NAME,
            "description": _DESCRIPTION,
            "parameters": podcast_parameters(),
        }
    ]


def _miss(error: str, shows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "shows": shows,
        "note": "shows is the Podcasts I follow order, top to bottom. Call again with a title from that list.",
    }


def _spoken_hit(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "announce": frame.get("announce") or "",
        "openSpotifyUri": frame.get("openSpotifyUri"),
        "spoken": True,
        "note": "The Portal already said this. Do not repeat it.",
    }


async def open_show(query: str, shows: list, post_open) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve [query] and, on one show, ask the router to open it.

    [post_open] takes the chosen title and returns the ``/api/podcasts/open`` body.
    The frame is None when nothing should be sent to the Portal.
    """
    plan = plan_open(query, shows)
    if plan["kind"] != "open":
        return plan["result"], None
    body = await post_open(plan["title"])
    if not isinstance(body, dict):
        listed = catalog(followed_order(shows))
        return _miss("I couldn't open that show.", listed), None
    frame = podcast_frame(body)
    if frame is None:
        message = str(body.get("announce") or body.get("error") or "No followed show matched.")
        return _miss(message, catalog(followed_order(shows))), None
    return _spoken_hit(frame), frame


def podcast_open_extension(esp32: Any) -> Any:
    """Voice-tool extension. Ok means the podcast frame was written."""
    from .voice_tools import ToolExtension, ToolMetadata

    declarations = tuple(podcast_declarations())

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name != PODCAST_TOOL_NAME:
            return {"ok": False, "error": "unknown podcast tool"}
        show = args.get("show")
        if not isinstance(show, str) or not show.strip():
            return {"ok": False, "error": "show is required"}
        try:
            shows = await _get_followed()
        except Exception:
            logger.warning("podcast followed list failed")
            return {"ok": False, "error": "I couldn't reach the podcast list."}
        try:
            result, frame = await open_show(show, shows, _post_open)
        except Exception:
            logger.warning("podcast open failed")
            return {"ok": False, "error": "I couldn't open that show."}
        if frame is None:
            return result
        send = getattr(esp32, "send_podcast", None)
        if not callable(send):
            return {"ok": False, "error": "podcast channel unavailable"}
        try:
            sent, error = await send(frame)
        except Exception:
            return {"ok": False, "error": "podcast frame failed"}
        if error or not (isinstance(sent, dict) and sent.get("ok") is True):
            message = "podcast frame failed"
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
            return {"ok": False, "error": message}
        logger.info("portal frame %s", json.dumps(frame, ensure_ascii=False))
        return result

    return ToolExtension(
        declarations=declarations,
        dispatch=dispatch,
        metadata={PODCAST_TOOL_NAME: ToolMetadata(True, "portal_podcast", 15.0)},
        instructions=PODCAST_INSTRUCTIONS,
    )


async def _get_followed() -> list:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{router_origin()}/api/podcasts/followed") as response:
            body = await response.json(content_type=None)
            status = response.status
    if status >= 400 or not isinstance(body, dict) or body.get("ok") is not True:
        raise RuntimeError("followed list refused")
    shows = body.get("shows")
    if not isinstance(shows, list):
        raise RuntimeError("followed list missing")
    return shows


async def _post_open(title: str) -> dict:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{router_origin()}/api/podcasts/open",
            json={"show": title},
        ) as response:
            body = await response.json(content_type=None)
    if not isinstance(body, dict):
        raise RuntimeError("open refused")
    return body
