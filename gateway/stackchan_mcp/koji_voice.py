# SPDX-License-Identifier: MIT
"""One-voice frames: stamp seq and voice, store screen context, wait for results.

Grok is the only voice when ``features.one_voice`` is set. The Portal stays silent.
These helpers never log row labels or the words of a result.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

ACCEPT_WAIT_S = 1.5

_SAY = {
    "opening": "Opening {show}.",
    "already_opening": "{show} is already opening.",
    "already_playing": "{show} is already playing.",
    "no_target": "No episode of {show}.",
    "stale": "That ask is already over.",
    "sent_unconfirmed": "Opening {show}.",
}


async def deliver_frame(send, frame: dict[str, Any], turn_seq: int | None):
    """Pass the seq captured when the tool started, when the sender accepts it."""
    if "turn_seq" in inspect.signature(send).parameters:
        return await send(frame, turn_seq=turn_seq)
    return await send(frame)


def context_seq(context: object) -> int | None:
    if not isinstance(context, dict):
        return None
    seq = context.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


def stamp_podcast_frame(frame: dict[str, Any], *, one_voice: bool, seq: int | None) -> dict[str, Any]:
    """Add ``voice`` and ``seq`` only after a one-voice hello."""
    if not one_voice:
        return frame
    stamped = dict(frame)
    stamped["voice"] = "grok"
    if isinstance(seq, int) and not isinstance(seq, bool):
        stamped["seq"] = seq
    return stamped


def stamp_device_command(frame: dict[str, Any], *, one_voice: bool, seq: int | None, command_id: str) -> dict[str, Any]:
    """Add ``id`` and ``seq`` only for a one-voice device."""
    if not one_voice:
        return frame
    stamped = dict(frame)
    stamped["id"] = command_id
    if isinstance(seq, int) and not isinstance(seq, bool):
        stamped["seq"] = seq
    return stamped


def say_hint(status: str, show: str) -> str:
    """At most eight words. The model says this; the Portal does not."""
    name = " ".join((show or "That show").split()) or "That show"
    text = _SAY.get(status, _SAY["opening"]).format(show=name)
    words = text.replace(".", "").split()
    if len(words) <= 8:
        return text if text.endswith(".") else text + "."
    return " ".join(words[:8]) + "."


def podcast_target(frame: dict[str, Any]) -> str:
    if frame.get("openSpotifyUri") or frame.get("openSpotifyUrl"):
        return "spotify"
    rss = frame.get("rssEpisode")
    if isinstance(rss, dict) and rss.get("enclosureUrl"):
        return "rss"
    return "rss"


def accept_result(status: str, show: str, frame: dict[str, Any], last: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": True,
        "status": status,
        "show": show,
        "target": podcast_target(frame),
        "say": say_hint(status, show),
    }
    if isinstance(last, dict) and last.get("outcome"):
        body["last_outcome"] = {"outcome": last.get("outcome"), "show": last.get("show") or ""}
    return body


def store_koji_context(message: dict[str, Any]) -> dict[str, Any] | None:
    """Keep the latest ask context. The log is seq, panel id, and row count."""
    if message.get("v") != 1:
        return None
    seq = message.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    panel = message.get("panel")
    stored_panel = None
    panel_id = "-"
    row_count = 0
    if isinstance(panel, dict):
        raw_id = panel.get("id")
        panel_id = raw_id if isinstance(raw_id, str) and raw_id else "-"
        title = panel.get("title") if isinstance(panel.get("title"), str) else ""
        rows_in = panel.get("rows") if isinstance(panel.get("rows"), list) else []
        rows = []
        for row in rows_in[:12]:
            if not isinstance(row, dict):
                continue
            label = row.get("label")
            if not isinstance(label, str) or not label.strip():
                continue
            number = row.get("n")
            item: dict[str, Any] = {
                "n": number if isinstance(number, int) and not isinstance(number, bool) else len(rows) + 1,
                "label": " ".join(label.split())[:120],
            }
            subtitle = row.get("subtitle")
            if isinstance(subtitle, str) and subtitle.strip():
                item["subtitle"] = " ".join(subtitle.split())[:120]
            rows.append(item)
        row_count = len(rows)
        stored_panel = {"id": panel_id if panel_id != "-" else "", "title": title[:120], "rows": rows}
        kind = panel.get("kind")
        if isinstance(kind, str) and kind.strip():
            stored_panel["kind"] = kind.strip()[:32]
    from .portal_state import OFFLINE, REFLEX, clean_events

    reflex = []
    raw_reflex = message.get("reflex")
    if isinstance(raw_reflex, list):
        for item in raw_reflex:
            if isinstance(item, str) and item in REFLEX and item not in reflex:
                reflex.append(item)
    offline = []
    raw_offline = message.get("offline_actions")
    if isinstance(raw_offline, list):
        for item in raw_offline:
            if isinstance(item, str) and item in OFFLINE and item not in offline:
                offline.append(item)
    logger.info(
        "koji_context seq=%s panel=%s rows=%s reflex=%s offline=%s",
        seq,
        panel_id,
        row_count,
        len(reflex),
        len(offline),
    )
    stored: dict[str, Any] = {"seq": seq, "panel": stored_panel}
    if reflex:
        stored["reflex"] = reflex
    if offline:
        stored["offline_actions"] = offline
    portal_state = message.get("portal_state")
    if isinstance(portal_state, dict):
        stored["portal_state"] = portal_state
    events = clean_events(message.get("portal_events"), limit=5)
    if events:
        stored["portal_events"] = events
    attempt = message.get("local_attempt")
    if isinstance(attempt, dict) and attempt.get("ok") is False:
        name = attempt.get("name") if isinstance(attempt.get("name"), str) else ""
        detail = attempt.get("detail") if isinstance(attempt.get("detail"), str) else ""
        stored["local_attempt"] = {"name": name[:40], "ok": False, "detail": detail[:80]}
    return stored


def final_instruction(outcome: str, show: str) -> str | None:
    """What Grok should say after the launch watch. None when nothing should be pushed."""
    name = " ".join((show or "That show").split()) or "That show"
    if outcome == "rss_playing":
        return (
            f"{name} did not start in Spotify; it is playing on the Portal speaker. "
            "Say that in one short sentence."
        )
    if outcome == "failed":
        return f"{name} did not start. Say that in one short sentence."
    return None
