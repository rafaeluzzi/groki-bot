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
    "On a miss the result lists the numbered titles and tells you to call this tool once more "
    "with the closest-sounding title, or to ask which show and name 2 or 3 of them. "
    "Do that before you speak. Do not say the request was not for you. "
    "A blank stored title is replaced by its feed title when that title can be read. "
    "A title that is still blank is last and has no number. "
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
    "If the tool says the Portal already said the announce line, do not repeat it. "
    "If matched is false, call podcast_open_spotify once more with the closest-sounding title "
    "in call_again before you speak. If there is no single closest title, ask which show and "
    "name the 2 or 3 candidates. Do not say the request was not for you. "
    "A blank stored title is not a numbered row. Use the feed title from shows when one is given."
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
    """Titled shows A-to-Z, then a still-blank title with no number.

    The same key as the router's ``followed()`` sort. A blank title sorts last
    so it does not become number 1.
    """
    rows = [show for show in shows if isinstance(show, dict)]

    def key(show: dict) -> tuple[bool, str]:
        title = str(show.get("title") or "").strip().lower()
        return (title == "", title)

    return sorted(rows, key=key)


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
    """Number titled rows only. A blank title is last and has no ``n``."""
    rows: list[dict[str, Any]] = []
    blank = 0
    number = 0
    for show in shows:
        title = str(show.get("title") or "").strip()
        if not title:
            blank += 1
            continue
        number += 1
        rows.append({"n": number, "title": title})
    rows.extend({"title": ""} for _ in range(blank))
    return rows


def _titled(shows: list[dict]) -> list[dict]:
    return [show for show in shows if str(show.get("title") or "").strip()]


def plan_open(query: str, shows: list) -> dict[str, Any]:
    """Pick a followed show, or a miss the model can correct.

    Title matches win over an ordinal, so a show named like a number stays a title.
    ``number N`` uses [followed_order], which is the panel order.
    """
    ordered = followed_order(shows)
    titled = _titled(ordered)
    listed = catalog(ordered)
    text = " ".join((query or "").split())
    if not text:
        return {"kind": "miss", "result": _miss("Which show?", listed)}
    matches = match_followed(text, titled)
    if len(matches) == 1:
        return {"kind": "open", "title": str(matches[0].get("title") or text)}
    if len(matches) > 1:
        names = ", ".join(str(show.get("title") or "a show") for show in matches[:3])
        return {"kind": "ask", "result": _miss(f"Which show? {names}.", listed)}
    number = ordinal_index(text)
    if number is None:
        return {"kind": "miss", "result": _miss("No followed title matched.", listed, query=text, retry=True)}
    if number > len(titled):
        return {"kind": "range", "result": _miss(f"There is no show number {number}.", listed)}
    return {"kind": "open", "title": str(titled[number - 1].get("title") or "")}


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


_STOP_WORDS = {"a", "and", "podcast", "podcasts", "the", "with"}
_CLEAR_SCORE = 0.5


def _skeleton(value: str) -> str:
    """Consonants only, so a mishearing can sit next to the stored title."""
    out: list[str] = []
    previous = ""
    for ch in title_key(value).replace(" ", ""):
        if ch in "aeiouy":
            continue
        if ch in "ckq":
            ch = "k"
        if ch == previous:
            continue
        out.append(ch)
        previous = ch
    return "".join(out)


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for index, ch in enumerate(left, start=1):
        current = [index]
        for other_index, other in enumerate(right, start=1):
            current.append(
                min(
                    previous[other_index] + 1,
                    current[other_index - 1] + 1,
                    previous[other_index - 1] + (ch != other),
                )
            )
        previous = current
    return previous[-1]


def _ratio(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    return 1.0 - _edit_distance(left, right) / max(len(left), len(right))


def sound_score(query: str, title: str) -> float:
    """How much [query] sounds like [title]. 1 is the same words."""
    query_words = [word for word in title_key(query).split() if word not in _STOP_WORDS]
    title_words = [word for word in title_key(title).split() if word not in _STOP_WORDS]
    if not query_words or not title_words:
        return 0.0

    def best(word: str, key) -> float:
        return max(_ratio(key(word), key(other)) for other in title_words)

    raw = sum(best(word, lambda text: text) for word in query_words) / len(query_words)
    sounded = sum(best(word, _skeleton) for word in query_words) / len(query_words)
    return 0.35 * raw + 0.65 * sounded


def closest_titles(query: str, shows: list[dict[str, Any]]) -> list[str]:
    """Up to three titles the miss should name. One when a single show is clearly closest."""
    titled = [str(row.get("title") or "").strip() for row in shows]
    titled = [title for title in titled if title]
    scored = sorted(
        ((sound_score(query, title), title) for title in titled),
        key=lambda item: (-item[0], item[1].lower()),
    )
    if not scored or scored[0][0] < 0.45:
        return titled[:3]
    top = scored[0][0]
    if top >= _CLEAR_SCORE and (len(scored) == 1 or top - scored[1][0] >= 0.12):
        return [scored[0][1]]
    return [title for score, title in scored if score >= max(0.45, top - 0.15)][:3]


def _miss(error: str, shows: list[dict[str, Any]], *, query: str = "", retry: bool = False) -> dict[str, Any]:
    """A miss the model can correct. [shows] is the numbered panel order."""
    candidates = closest_titles(query, shows) if retry and query.strip() else []
    call_again = candidates[0] if len(candidates) == 1 and retry else ""
    if call_again:
        instruction = (
            f"No followed title matched. The closest-sounding show is {call_again}. "
            "Call podcast_open_spotify once more with show set to that exact title. "
            "Do not speak before that call returns, and do not say the request was not for you."
        )
    elif retry:
        names = ", ".join(candidates[:3]) or "the titles in shows"
        instruction = (
            "No followed title matched. Ask which show, and name these: "
            f"{names}. Do not say the request was not for you."
        )
    else:
        instruction = error
    body: dict[str, Any] = {
        "ok": False,
        "matched": False,
        "error": instruction,
        "shows": shows,
        "instruction": instruction,
    }
    if candidates:
        body["candidates"] = candidates
    if call_again:
        body["call_again"] = call_again
    return body


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
            shows = await fill_blank_titles(await _get_followed())
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
        metadata={PODCAST_TOOL_NAME: ToolMetadata(True, "portal_podcast", 30.0)},
        instructions=PODCAST_INSTRUCTIONS,
    )


_CHANNEL_TITLE = re.compile(rb"<channel\b[^>]*>.*?<title>([^<]{1,200})</title>", re.IGNORECASE | re.DOTALL)
_FEED_TITLE_CACHE: dict[str, str] = {}


def channel_title(payload: bytes) -> str:
    """The channel ``<title>`` in the first part of a feed. Empty when it is not there.

    Same read as the router's oversized-feed fallback: the title is near the top,
    even when the rest of the feed is too large to store.
    """
    if not payload:
        return ""
    match = _CHANNEL_TITLE.search(payload[:65536])
    if match is None:
        return ""
    text = match.group(1).decode("utf-8", "replace")
    return " ".join(text.replace("\x00", " ").split())


async def fill_blank_titles(shows: list, read_title=None) -> list:
    """Copy [shows], replacing a blank title with the feed's channel title."""
    reader = read_title or _feed_channel_title
    filled = []
    for show in shows:
        if not isinstance(show, dict):
            continue
        if str(show.get("title") or "").strip():
            filled.append(show)
            continue
        url = str(show.get("feedUrl") or "").strip()
        title = ""
        if url:
            try:
                title = str(await reader(url) or "").strip()
            except Exception:
                logger.warning("podcast feed title failed")
                title = ""
        if not title:
            filled.append(show)
            continue
        copied = dict(show)
        copied["title"] = title
        filled.append(copied)
    return filled


async def _feed_channel_title(url: str) -> str:
    cached = _FEED_TITLE_CACHE.get(url)
    if cached is not None:
        return cached
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=8)
    title = ""
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "KojiPortal/0.1 (podcasts; public RSS)"}) as response:
                if response.status < 400:
                    title = channel_title(await response.content.read(65536))
    except Exception:
        title = ""
    _FEED_TITLE_CACHE[url] = title
    return title


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
