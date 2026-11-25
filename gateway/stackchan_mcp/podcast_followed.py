# SPDX-License-Identifier: MIT
"""Read the followed-show list. Title and subtitle only, in the router's A-Z order."""

from __future__ import annotations

import logging
from typing import Any

from .podcast_open import followed_order, router_origin

logger = logging.getLogger(__name__)

FOLLOWED_TOOL = "podcast_followed"

_DESCRIPTION = (
    "List the podcasts this Portal follows. "
    "Returns title and subtitle only, A-Z, the same order podcast_open_spotify uses. "
    "Before you list followed shows or build a followed-shows panel, call podcast_followed "
    "and use only its result, even if the user names specific shows. "
    "This does not speak and does not open a show."
)


def followed_rows(shows: list) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for show in followed_order(shows):
        title = str(show.get("title") or "").strip()
        if not title:
            continue
        subtitle = str(show.get("subtitle") or show.get("author") or "").strip()
        item = {"title": title}
        if subtitle:
            item["subtitle"] = subtitle
        rows.append(item)
    return rows


async def fetch_followed() -> list[dict[str, str]]:
    from .podcast_open import _get_followed

    try:
        shows = await _get_followed()
    except Exception:
        logger.info("podcast_followed ok=false")
        raise
    rows = followed_rows(shows)
    logger.info("podcast_followed ok=true n=%s", len(rows))
    return rows


def podcast_followed_extension() -> Any:
    from .voice_tools import ToolExtension, ToolMetadata

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        del args
        if name != FOLLOWED_TOOL:
            return {"ok": False, "error": "unknown podcast tool"}
        try:
            rows = await fetch_followed()
        except Exception:
            return {"ok": False, "error": "followed list unavailable"}
        return {"ok": True, "shows": rows}

    return ToolExtension(
        declarations=(
            {
                "name": FOLLOWED_TOOL,
                "description": _DESCRIPTION,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ),
        dispatch=dispatch,
        metadata={FOLLOWED_TOOL: ToolMetadata(True, "portal_podcast", 10.0)},
        instructions="",
    )


def router_base() -> str:
    return router_origin()
