# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Koji wiki load, voice instructions, presence files, and the read-only HTTP surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import websockets

from stackchan_mcp.capture_server import create_capture_app
from stackchan_mcp.esp32_client import ESP32Manager
from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge
from stackchan_mcp.gbot_voice_proxy import GbotVoiceProxy
from stackchan_mcp.koji_wiki import (
    handle_wiki_manifest,
    handle_wiki_page,
    load_bundle,
    load_instruction_text,
    publish_bridge_presence,
    record_portal_presence,
)

_MANIFEST = {
    "version": 1,
    "pages": [
        {"id": "roles", "path": "pages/roles.md"},
        {"id": "handoff", "path": "pages/handoff.md"},
        {"id": "catalog", "path": "pages/catalog.md"},
    ],
}


class _PageRequest:
    def __init__(self, page_id: str) -> None:
        self.match_info = {"page_id": page_id}


def _write_wiki(root: Path, *, roles: str, handoff: str, catalog: str = "catalog pointer\n") -> None:
    pages = root / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "roles.md").write_text(roles, encoding="utf-8")
    (pages / "handoff.md").write_text(handoff, encoding="utf-8")
    (pages / "catalog.md").write_text(catalog, encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps(_MANIFEST), encoding="utf-8")


def _bridge() -> GbotVoiceBridge:
    return GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=object(),
    )


def test_seeded_pages_are_loaded_into_login_voice_instructions(monkeypatch):
    monkeypatch.delenv("STACKCHAN_KOJI_WIKI", raising=False)
    monkeypatch.delenv("STACKCHAN_KOJI_WIKI_DIR", raising=False)
    monkeypatch.delenv("STACKCHAN_DEVICE_COMMANDS", raising=False)
    bridge = _bridge()
    names = {item["name"] for item in bridge._configure_tools({"tools": []})}
    text = bridge._provider_tool_instructions()
    assert "device_command" in names
    assert "two execution seats" in text
    assert "Closed command that matches on-device grammar" in text
    assert "Device command catalog (pointer)" in text
    assert "template" in text
    assert "weather" in text
    assert "core_morph" in text
    assert "get_current_datetime" in text
    assert "end_the_call" in text
    assert "Never call set_volume, media_control, open_app" in text
    assert "# Presence" not in text
    assert not any("wiki" in name for name in names)


def test_instructions_follow_markdown_on_disk(monkeypatch, tmp_path: Path):
    _write_wiki(
        tmp_path,
        roles="# Roles\n\nMARKER_ROLE_SEAT\n",
        handoff="# Handoff\n\nMARKER_HANDOFF_STEP\n",
    )
    monkeypatch.setenv("STACKCHAN_KOJI_WIKI_DIR", str(tmp_path))
    monkeypatch.delenv("STACKCHAN_KOJI_WIKI", raising=False)
    first = _bridge()
    first._configure_tools({"tools": []})
    text = first._provider_tool_instructions()
    assert "MARKER_ROLE_SEAT" in text
    assert "MARKER_HANDOFF_STEP" in text

    (tmp_path / "pages" / "roles.md").write_text("# Roles\n\nMARKER_ROLE_UPDATED\n", encoding="utf-8")
    second = _bridge()
    second._configure_tools({"tools": []})
    updated = second._provider_tool_instructions()
    assert "MARKER_ROLE_UPDATED" in updated
    assert "MARKER_ROLE_SEAT" not in updated


def test_wiki_flag_leaves_injection_off(monkeypatch):
    monkeypatch.setenv("STACKCHAN_KOJI_WIKI", "0")
    monkeypatch.delenv("STACKCHAN_KOJI_WIKI_DIR", raising=False)
    bridge = _bridge()
    bridge._configure_tools({"tools": []})
    text = bridge._provider_tool_instructions()
    assert "two execution seats" not in text
    assert "Closed command that matches on-device grammar" not in text
    assert "stay on this device" in text


def test_presence_summary_is_one_line(monkeypatch, tmp_path: Path):
    _write_wiki(tmp_path, roles="# Roles\n\nseats\n", handoff="# Handoff\n\nsteps\n")
    assert record_portal_presence(
        {
            "type": "koji_presence",
            "presence": {
                "portal_online": True,
                "spotify_playing": True,
                "wiki_etag": "secret-etag",
            },
        },
        tmp_path,
    )
    monkeypatch.setenv("STACKCHAN_KOJI_WIKI_DIR", str(tmp_path))
    monkeypatch.delenv("STACKCHAN_KOJI_WIKI", raising=False)
    text = load_instruction_text()
    assert "Presence: Portal online; Spotify playing." in text
    assert "secret-etag" not in text
    assert "wiki_etag" not in text


def test_presence_frame_persists_the_last_body(tmp_path: Path):
    first = {
        "type": "koji_presence",
        "presence": {
            "portal_online": True,
            "voice_turn": True,
            "ts": "2026-11-03T18:00:00Z",
        },
    }
    assert record_portal_presence(first, tmp_path) is True
    saved = json.loads((tmp_path / "presence" / "portal.json").read_text(encoding="utf-8"))
    assert saved == first["presence"]

    assert record_portal_presence(
        {"type": "koji_presence", "presence": {"note": "this is speech"}, "session_id": "x"},
        tmp_path,
    ) is False
    assert json.loads((tmp_path / "presence" / "portal.json").read_text(encoding="utf-8")) == first["presence"]

    assert record_portal_presence(
        {"type": "koji_presence", "presence": {"portal_online": False, "spotify_playing": True}},
        tmp_path,
    ) is True
    replaced = json.loads((tmp_path / "presence" / "portal.json").read_text(encoding="utf-8"))
    assert replaced["portal_online"] is False
    assert replaced["spotify_playing"] is True
    assert "voice_turn" not in replaced
    assert replaced["ts"].endswith("Z")


def test_manifest_etag_tracks_page_bytes(tmp_path: Path):
    _write_wiki(tmp_path, roles="one\n", handoff="hand\n")
    first = load_bundle(tmp_path)
    assert first is not None
    (tmp_path / "pages" / "roles.md").write_text("two\n", encoding="utf-8")
    second = load_bundle(tmp_path)
    assert second is not None
    assert first.etag != second.etag
    assert first.etag.startswith('"sha256-')
    assert first.pages["roles"].etag != second.pages["roles"].etag


def test_bridge_presence_file_follows_voice_health(monkeypatch, tmp_path: Path):
    _write_wiki(tmp_path, roles="roles\n", handoff="handoff\n")
    monkeypatch.setenv("STACKCHAN_KOJI_WIKI_DIR", str(tmp_path))
    assert publish_bridge_presence(online=True, root=tmp_path) is True
    saved = json.loads((tmp_path / "presence" / "bridge.json").read_text(encoding="utf-8"))
    assert saved["bridge_online"] is True
    assert saved["ts"].endswith("Z")
    assert saved["wiki_etag"].startswith('"sha256-')

    proxy = GbotVoiceProxy()
    proxy._record_connection(False, "connection_lost")
    replaced = json.loads((tmp_path / "presence" / "bridge.json").read_text(encoding="utf-8"))
    assert replaced["bridge_online"] is False


def test_capture_app_registers_wiki_routes():
    paths = set()
    for route in create_capture_app().router.routes():
        resource = getattr(route, "resource", None)
        if resource is not None:
            paths.add(resource.canonical)
    assert "/wiki/manifest" in paths
    assert "/wiki/pages/{page_id}" in paths


@pytest.mark.asyncio
async def test_http_manifest_and_page_without_a_server(monkeypatch):
    monkeypatch.delenv("STACKCHAN_KOJI_WIKI_DIR", raising=False)
    monkeypatch.setenv("STACKCHAN_KOJI_WIKI", "0")
    manifest = await handle_wiki_manifest(None)
    assert manifest.status == 200
    body = json.loads(manifest.text)
    assert body["version"] == 1
    assert [page["id"] for page in body["pages"]] == ["roles", "handoff", "panels", "presence", "catalog"]
    assert manifest.headers["ETag"] == body["etag"]
    roles_etag = next(page["etag"] for page in body["pages"] if page["id"] == "roles")

    page = await handle_wiki_page(_PageRequest("roles"))
    assert page.status == 200
    assert page.content_type == "text/markdown"
    assert "two execution seats" in page.text
    assert page.headers["ETag"] == roles_etag

    handoff = await handle_wiki_page(_PageRequest("handoff"))
    assert handoff.status == 200
    assert "Closed command that matches on-device grammar" in handoff.text

    missing = await handle_wiki_page(_PageRequest("nope"))
    assert missing.status == 404
    escaped = await handle_wiki_page(_PageRequest("../roles"))
    assert escaped.status == 404


@pytest.mark.asyncio
async def test_koji_presence_is_stored_and_not_forwarded(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("STACKCHAN_KOJI_WIKI_DIR", str(tmp_path))

    class RecordingProxy:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        async def start(self, info, hello, send):
            return True

        async def stop(self):
            return None

        async def send_device_json(self, message):
            self.messages.append(message)
            return True

        async def try_handle_device_mcp(self, message):
            return False

    proxy = RecordingProxy()
    manager = ESP32Manager(cloud_proxy_factory=lambda: proxy)
    await manager.start("127.0.0.1", 0)
    port = manager._server.sockets[0].getsockname()[1]
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "version": 1,
                "features": {"mcp": False},
            }))
            await asyncio.wait_for(ws.recv(), timeout=5.0)
            await asyncio.sleep(0.1)
            await ws.send(json.dumps({
                "type": "koji_presence",
                "presence": {"note": "not speech"},
            }))
            await ws.send(json.dumps({
                "type": "koji_presence",
                "presence": {"portal_online": True, "voice_turn": False, "spotify_playing": True},
            }))
            await ws.send(json.dumps({"type": "listen", "state": "stop"}))
            await asyncio.sleep(0.1)
        assert proxy.messages == [{"type": "listen", "state": "stop"}]
        saved = json.loads((tmp_path / "presence" / "portal.json").read_text(encoding="utf-8"))
        assert saved["portal_online"] is True
        assert saved["voice_turn"] is False
        assert saved["spotify_playing"] is True
        assert "note" not in saved
    finally:
        await manager.stop()
