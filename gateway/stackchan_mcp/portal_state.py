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
    "conversation",
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


def _at_ms(status: dict[str, Any]) -> int | None:
    value = status.get("at_ms")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def accept_status(
    current: dict[str, Any] | None,
    incoming: object,
    *,
    fresh_session: bool = False,
) -> dict[str, Any] | None:
    """Higher rev wins inside one epoch.

    A different epoch is a new Portal process (reinstall or cleared app data).
    Accept it when its at_ms is newer, or when it is the first status of a new session.
    """
    if not isinstance(incoming, dict):
        return current
    rev = incoming.get("rev")
    if isinstance(rev, bool) or not isinstance(rev, int):
        return current
    if current is None or fresh_session:
        return incoming
    same = incoming.get("epoch") == current.get("epoch")
    old = current.get("rev")
    old_rev = old if isinstance(old, int) and not isinstance(old, bool) else None
    if same:
        if old_rev is not None and rev <= old_rev:
            return current
        return incoming
    new_at = _at_ms(incoming)
    old_at = _at_ms(current)
    if new_at is not None and old_at is not None and new_at >= old_at:
        return incoming
    if old_rev is None or rev > old_rev:
        return incoming
    return current


def panel_matches(status: dict[str, Any] | None, *, show_id: str | None, dismiss_id: str | None) -> bool:
    """True when a later portal_status shows the panel we just opened or closed."""
    panel = status.get("panel") if isinstance(status, dict) else None
    if not isinstance(panel, dict):
        return False
    if show_id:
        current = panel.get("current")
        return isinstance(current, dict) and current.get("id") == show_id
    if dismiss_id:
        closed = panel.get("last_closed")
        return isinstance(closed, dict) and closed.get("id") == dismiss_id
    return False


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
