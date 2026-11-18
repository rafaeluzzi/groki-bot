# SPDX-License-Identifier: MIT
"""Follow and unfollow shows. The Portal does not speak and no frame is sent.

Search goes through the router. A show found this way often has no Spotify
show URI, so playback falls back to the public RSS enclosure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .podcast_open import match_followed, router_origin
from .podcast_turn import context_panel, shared_guard, turn_key

logger = logging.getLogger(__name__)

FOLLOW_BATCH_S = 20.0
# The router uses 8 s to search and 10 s to follow. Stay under the batch cap.
FOLLOW_CLIENT_S = 19.0
_FOLLOW_SLOTS = 4

FOLLOW_TOOL = "podcast_follow"
UNFOLLOW_TOOL = "podcast_unfollow"

FOLLOW_INSTRUCTIONS_ONE_VOICE = (
    "Call podcast_follow or podcast_unfollow before speaking. "
    "After it returns, say one short line. The Portal does not speak and nothing opens. "
    "podcast_follow takes shows, a list of one to eight titles, or from_screen true "
    "to follow the labels on the list that is up. "
    "One podcast_follow per turn. A second call returns already_handled. "
    "A show followed this way may have no Spotify link, so it plays from the public RSS feed."
)

FOLLOW_INSTRUCTIONS = (
    "To follow or unfollow, use podcast_follow and podcast_unfollow. "
    "Never call podcast_open_spotify to follow. "
    "podcast_follow takes shows, a list of one to eight titles, or from_screen true "
    "to follow the labels on the list that is up. "
    "Say one short summary from the result. The Portal does not speak and nothing opens. "
    "A show followed this way may have no Spotify link, so it plays from the public RSS feed."
)


def follow_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "shows": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One to eight show titles to follow.",
            },
            "from_screen": {
                "type": "boolean",
                "description": "Follow the labels on the list that is on screen. Ignores shows.",
            },
        },
        "additionalProperties": False,
    }


def unfollow_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "show": {"type": "string", "description": "Followed show title to unfollow."},
        },
        "required": ["show"],
        "additionalProperties": False,
    }


def panel_labels(panel: dict[str, Any] | None, limit: int = 8) -> list[str]:
    if not isinstance(panel, dict):
        return []
    rows = panel.get("rows")
    if not isinstance(rows, list):
        return []
    labels: list[str] = []
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        label = " ".join(str(row.get("label") or "").split())
        if not label:
            continue
        labels.append(label[:120])
        if len(labels) >= limit:
            break
    return labels


def follow_summary(results: list[dict[str, Any]]) -> str:
    groups = {
        "followed": "Followed",
        "already": "Already following",
        "not_found": "Couldn't find",
        "ambiguous": "Need a clearer name for",
        "pending": "Still adding",
    }
    parts = []
    for status, prefix in groups.items():
        names = [str(row.get("title") or "") for row in results if row.get("status") == status]
        names = [name for name in names if name]
        if names:
            parts.append(prefix + " " + ", ".join(names) + ".")
    return " ".join(parts) or "Nothing to follow."


def podcast_follow_extension(esp32: Any) -> Any:
    from .voice_tools import ToolExtension, ToolMetadata

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        guard = shared_guard(esp32)
        async with guard.lock:
            if name == FOLLOW_TOOL:
                return await _follow(esp32, args, guard)
            if name == UNFOLLOW_TOOL:
                return await _unfollow(args)
            return {"ok": False, "error": "unknown podcast tool"}

    return ToolExtension(
        declarations=(
            {
                "name": FOLLOW_TOOL,
                "description": (
                    "Follow one to eight podcasts. from_screen follows the list on screen. "
                    "Does not open Spotify and does not speak. "
                    "A followed show may have no Spotify link and then plays from RSS. "
                    + FOLLOW_INSTRUCTIONS
                ),
                "parameters": follow_parameters(),
            },
            {
                "name": UNFOLLOW_TOOL,
                "description": "Stop following one podcast. Does not speak. " + FOLLOW_INSTRUCTIONS,
                "parameters": unfollow_parameters(),
            },
        ),
        dispatch=dispatch,
        metadata={
            FOLLOW_TOOL: ToolMetadata(True, "portal_podcast", 30.0),
            UNFOLLOW_TOOL: ToolMetadata(True, "portal_podcast", 30.0),
        },
        instructions=FOLLOW_INSTRUCTIONS,
        instructions_for=lambda: (
            FOLLOW_INSTRUCTIONS_ONE_VOICE
            if getattr(esp32, "one_voice", False) is True
            else FOLLOW_INSTRUCTIONS
        ),
    )


def _follow_titles(esp32: Any, args: dict[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
    if args.get("from_screen") is True:
        titles = panel_labels(context_panel(esp32))
        if not titles:
            return [], {"ok": False, "reason": "no_panel"}
        return titles, None
    raw = args.get("shows")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        return [], {"ok": False, "error": "shows must be 1 to 8 titles"}
    titles = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return [], {"ok": False, "error": "shows must be titles"}
        titles.append(" ".join(item.split()))
    return titles, None


async def _follow(esp32: Any, args: dict[str, Any], guard: Any) -> dict[str, Any]:
    titles, problem = _follow_titles(esp32, args)
    if problem is not None:
        return problem
    rejection = guard.claim_follow(turn_key(esp32))
    if rejection is not None:
        return rejection
    semaphore = asyncio.Semaphore(_FOLLOW_SLOTS)

    async def one(title: str) -> dict[str, Any]:
        async with semaphore:
            try:
                body = await _post_follow(title)
            except TimeoutError:
                logger.warning("podcast follow still running")
                body = {"status": "pending"}
            except Exception:
                logger.warning("podcast follow failed")
                body = {"status": "not_found"}
        status = body.get("status") if isinstance(body, dict) else None
        if status not in {"followed", "already", "not_found", "ambiguous", "pending"}:
            status = "not_found"
        row: dict[str, Any] = {"title": title, "status": status}
        names = body.get("names") if isinstance(body, dict) else None
        if status == "ambiguous" and isinstance(names, list):
            row["names"] = [str(name) for name in names[:3] if isinstance(name, str)]
        return row

    pairs = [(title, asyncio.create_task(one(title))) for title in titles]
    done, unfinished = await asyncio.wait([task for _, task in pairs], timeout=FOLLOW_BATCH_S)
    if unfinished:
        logger.warning("podcast follow batch timed out")
    finished = {}
    for task in done:
        try:
            row = task.result()
        except Exception:
            logger.warning("podcast follow failed")
            continue
        finished[task] = row
    results = []
    for title, task in pairs:
        if task in finished:
            results.append(finished[task])
        else:
            results.append({"title": title, "status": "pending"})
    # The reply already says pending. These are the client waits, not the router follow.
    for task in unfinished:
        task.cancel()
    if unfinished:
        await asyncio.gather(*unfinished, return_exceptions=True)
    return {"ok": True, "results": results, "say": follow_summary(results)}


async def _unfollow(args: dict[str, Any]) -> dict[str, Any]:
    show = args.get("show")
    if not isinstance(show, str) or not show.strip():
        return {"ok": False, "error": "show is required"}
    query = " ".join(show.split())
    try:
        shows = await _get_followed()
    except Exception:
        logger.warning("podcast unfollow list failed")
        return {"ok": False, "error": "I couldn't reach the podcast list."}
    matches = match_followed(query, [row for row in shows if isinstance(row, dict)])
    if not matches:
        return {"ok": False, "matched": False, "reason": "not_followed", "query": query}
    if len(matches) > 1:
        names = [str(row.get("title") or "") for row in matches[:3]]
        return {"ok": False, "reason": "ambiguous", "names": [name for name in names if name]}
    chosen = matches[0]
    show_id = str(chosen.get("id") or "")
    if not show_id:
        return {"ok": False, "reason": "not_followed", "query": query}
    try:
        await _delete_follow(show_id)
    except Exception:
        logger.warning("podcast unfollow failed")
        return {"ok": False, "error": "I couldn't unfollow that show."}
    return {"ok": True, "status": "unfollowed", "title": str(chosen.get("title") or query)}


async def _post_follow(title: str) -> dict:
    import aiohttp

    # Longer than the router's search plus follow, and still inside the batch cap.
    timeout = aiohttp.ClientTimeout(total=FOLLOW_CLIENT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{router_origin()}/api/podcasts/follow_by_title",
            json={"title": title},
        ) as response:
            body = await response.json(content_type=None)
            status = response.status
    if status >= 400 or not isinstance(body, dict):
        return {"status": "not_found"}
    return body


async def _get_followed() -> list:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=8)
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


async def _delete_follow(show_id: str) -> None:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.delete(f"{router_origin()}/api/podcasts/follow/{show_id}") as response:
            if response.status >= 400:
                raise RuntimeError("unfollow refused")
