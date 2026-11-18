# SPDX-License-Identifier: MIT
"""get_portal_state: the live Portal, then the router, then the last snapshot."""

from __future__ import annotations

import logging
import os
from typing import Any

from .podcast_open import router_origin
from .portal_state import accept_status, mark_stale

logger = logging.getLogger(__name__)

GET_STATE_TOOL = "get_portal_state"


class PortalStatusCache:
    def __init__(self) -> None:
        self.status: dict[str, Any] | None = None
        self.source = ""

    def clear(self) -> None:
        self.status = None
        self.source = ""

    def offer(self, incoming: object, *, source: str) -> bool:
        nxt = accept_status(self.status, incoming)
        if nxt is None or nxt is self.status:
            return False
        self.status = nxt
        self.source = source
        return True


async def fetch_router_status() -> dict[str, Any]:
    import aiohttp

    token = os.getenv("STACKCHAN_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    timeout = aiohttp.ClientTimeout(total=1.5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{router_origin()}/api/portal/status", headers=headers) as response:
            body = await response.json(content_type=None)
    if not isinstance(body, dict):
        raise RuntimeError("router status")
    return body


def router_view(body: dict[str, Any]) -> dict[str, Any]:
    status = body.get("status") if isinstance(body.get("status"), dict) else {}
    out = dict(status)
    out["stale"] = True
    if isinstance(body.get("age_ms"), int) and not isinstance(body.get("age_ms"), bool):
        out["age_ms"] = body["age_ms"]
    out["source"] = "router"
    return out


async def read_portal_state(esp: Any, cache: PortalStatusCache) -> dict[str, Any]:
    connected = bool(getattr(esp, "device_connected", False) or getattr(esp, "connected", False))
    if connected:
        send = getattr(esp, "send_device_command", None)
        if callable(send):
            try:
                result, error = await send({"type": "device_command", "name": "get_state", "args": {}})
            except Exception:
                result, error = None, {"message": "send failed"}
            if error is None and isinstance(result, dict) and isinstance(result.get("portal_state"), dict):
                state = result["portal_state"]
                if state.get("stale") is not True:
                    cache.offer(state, source="portal")
                    fresh = dict(state)
                    fresh["stale"] = False
                    fresh["source"] = "portal"
                    return fresh
    try:
        body = await fetch_router_status()
    except Exception:
        logger.info("portal_state source=cache stale=true")
        cached = mark_stale(cache.status) or {"stale": True, "source": "cache"}
        cached = dict(cached)
        cached["stale"] = True
        cached.setdefault("source", "cache")
        return cached
    view = router_view(body)
    if isinstance(body.get("status"), dict):
        cache.offer(body["status"], source="router")
    logger.info("portal_state source=router stale=true")
    return view


def get_portal_state_extension(esp: Any, cache: PortalStatusCache) -> Any:
    from .voice_tools import ToolExtension, ToolMetadata

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        del args
        if name != GET_STATE_TOOL:
            return {"ok": False, "error": "unknown portal tool"}
        state = await read_portal_state(esp, cache)
        return {
            "ok": True,
            "confirmed": state.get("stale") is not True,
            "state": "done",
            "portal_state": state,
        }

    return ToolExtension(
        declarations=(
            {
                "name": GET_STATE_TOOL,
                "description": (
                    "Read the Portal status document. "
                    "Use it for what is playing, the volume, and whether a panel is open. "
                    "If stale is true, say so before relying on it. This does not speak."
                ),
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ),
        dispatch=dispatch,
        metadata={GET_STATE_TOOL: ToolMetadata(True, "portal_state", 2.0)},
        instructions="",
    )
