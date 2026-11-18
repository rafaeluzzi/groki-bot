# SPDX-License-Identifier: MIT
"""Cached Portal status. Titles stay in the snapshot Grok sees and never in logs."""

from __future__ import annotations

import json
from typing import Any

REFLEX = ("ducked", "grok_cut", "launch_cancelled")
OFFLINE = (
    "media_pause",
    "media_resume",
    "media_stop",
    "media_skip_next",
    "media_skip_previous",
    "volume_up",
    "volume_down",
    "set_volume_percent",
)
EVENT_TYPES = (
    "panel_opened",
    "panel_closed",
    "panel_waiting",
    "panel_changed",
    "playback_started",
    "playback_paused",
    "playback_ended",
    "late_spotify",
    "reflex",
    "offline_command",
    "bridge_up",
    "bridge_down",
    "action_final",
)
TITLE_KEYS = {"show", "episode", "title", "uri", "url", "enclosureUrl", "openSpotifyUri", "openSpotifyUrl"}


def enum_list(value: object, allowed: tuple[str, ...], *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item in allowed and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def clean_events(value: object, *, limit: int = 5) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or item.get("what")
        if not isinstance(kind, str) or kind not in (*EVENT_TYPES, *REFLEX, *OFFLINE):
            continue
        kept: dict[str, Any] = {}
        for key, raw in item.items():
            if key in TITLE_KEYS:
                continue
            if key in {"rev", "type", "what", "result", "action_id"} or key == "delta":
                kept[key] = raw if key != "delta" else _delta(raw)
        if kept:
            out.append(kept)
        if len(out) >= limit:
            break
    return out


def _delta(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: raw for key, raw in value.items() if key not in TITLE_KEYS and not _looks_like_uri(raw)}


def _looks_like_uri(value: object) -> bool:
    return isinstance(value, str) and ("://" in value or value.startswith("spotify:"))


def accept_status(current: dict[str, Any] | None, incoming: object) -> dict[str, Any] | None:
    """A higher rev wins. A lower rev is ignored. Epoch is recorded and is not an order."""
    if not isinstance(incoming, dict):
        return current
    rev = incoming.get("rev")
    if isinstance(rev, bool) or not isinstance(rev, int):
        return current
    if current is not None:
        old = current.get("rev")
        if isinstance(old, int) and not isinstance(old, bool) and rev < old:
            return current
    return incoming


def mark_stale(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return None
    copy = dict(status)
    copy["stale"] = True
    return copy


def parse_volume(detail: str) -> dict[str, int] | None:
    """`level N max M percent P` from the Portal volume result."""
    parts = detail.split()
    try:
        if len(parts) != 6 or parts[0] != "level" or parts[2] != "max" or parts[4] != "percent":
            return None
        return {"step": int(parts[1]), "max": int(parts[3]), "percent": int(parts[5])}
    except ValueError:
        return None


def tool_result(
    body: dict[str, Any],
    *,
    portal_state: dict[str, Any] | None,
    action_id: str = "",
    state: str = "",
    events: list[dict[str, Any]] | None = None,
    deduped: bool = False,
) -> dict[str, Any]:
    out = dict(body)
    if action_id:
        out["action_id"] = action_id
    if state:
        out["state"] = state
    if deduped:
        out["deduped"] = True
    if isinstance(portal_state, dict):
        out["portal_state"] = portal_state
    if events:
        out["portal_events"] = events
    return out


def attach_portal(esp: Any, payload: dict[str, Any], *, action_id: str = "", state: str = "done") -> dict[str, Any]:
    """Add the cached snapshot and any events the silent injector could not send."""
    if getattr(esp, "one_voice", False) is not True:
        return payload
    out = dict(payload)
    out.setdefault("confirmed", out.get("ok") is True)
    if action_id:
        out["action_id"] = action_id
        out.setdefault("state", state)
    cache = getattr(esp, "portal_cache", None)
    status = getattr(cache, "status", None) if cache is not None else None
    if isinstance(status, dict) and "portal_state" not in out:
        out["portal_state"] = status
    pending = getattr(esp, "pending_portal_events", None)
    if isinstance(pending, list) and pending:
        out["portal_events"] = list(pending)
        esp.pending_portal_events = []
    return out


def status_public(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return None
    return json.loads(json.dumps(status))
