# SPDX-License-Identifier: MIT
"""roku_play posts one router request and does not touch the Mac."""

import json
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge
from stackchan_mcp.roku_play import (
    ROKU_INSTRUCTIONS,
    play_payload,
    post_play,
    roku_play_enabled,
    roku_play_extension,
)


def test_instructions_say_the_action_and_refuse_a_silent_purchase():
    text = ROKU_INSTRUCTIONS
    assert "roku_play" in text
    assert "spoken_summary" in text
    assert "Playing Dune: Part Two on Max" in text
    assert "needs_confirm is ambiguous" in text
    assert "needs_confirm is rent" in text
    assert "Never rent or buy without a yes" in text
    assert "Do not open the title on the Mac" in text
    assert "projector" in text


def test_payload_keeps_only_the_spoken_fields():
    assert play_payload({"title": " Dune "}) == {"title": "Dune"}
    assert play_payload(
        {"title": "Dune", "service": "Max", "type": "movie", "year": 2021}
    ) == {"title": "Dune", "service": "Max", "type": "movie", "year": 2021}
    assert play_payload({"title": ""}) is None
    assert play_payload({"title": "Dune", "type": "episode"}) is None
    assert play_payload({"title": "Dune", "year": "2021"}) is None


def test_the_tool_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("STACKCHAN_ROKU_PLAY", "0")
    assert roku_play_enabled() is False
    monkeypatch.setenv("STACKCHAN_ROKU_PLAY", "on")
    assert roku_play_enabled() is True


@pytest.mark.asyncio
async def test_extension_posts_play_and_returns_the_router_summary(monkeypatch):
    posted = []

    async def fake_post(payload):
        posted.append(payload)
        return {
            "ok": True,
            "played": False,
            "state": "launched",
            "spoken_summary": "Playing Dune on Max.",
            "provider": "max",
            "title": "Dune",
        }

    monkeypatch.setattr("stackchan_mcp.roku_play.post_play", fake_post)
    extension = roku_play_extension()
    assert [entry["name"] for entry in extension.declarations] == ["roku_play"]
    result = await extension.dispatch(
        "roku_play",
        {"title": "Dune", "service": "Max", "type": "movie", "year": 2021},
    )
    assert result["spoken_summary"] == "Playing Dune on Max."
    assert posted == [{"title": "Dune", "service": "Max", "type": "movie", "year": 2021}]
    missing = await extension.dispatch("roku_play", {})
    assert missing == {"ok": False, "error": "title is required"}
    assert len(posted) == 1
    other = await extension.dispatch("device_command", {"title": "Dune"})
    assert other["ok"] is False


@pytest.mark.asyncio
async def test_a_down_router_is_a_spoken_failure(monkeypatch):
    class Broken:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise OSError("down")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("aiohttp.ClientSession", Broken)
    result = await post_play({"title": "Dune"})
    assert result["ok"] is False
    assert result["spoken_summary"] == "I can't reach the TV lookup."
    assert "down" not in json.dumps(result)


@pytest.mark.asyncio
async def test_gbot_voice_declares_roku_play(monkeypatch):
    monkeypatch.delenv("STACKCHAN_TOOL_BOT", raising=False)
    monkeypatch.delenv("STACKCHAN_TOOL_BOT_ID", raising=False)
    monkeypatch.delenv("STACKCHAN_ROKU_PLAY", raising=False)
    posted = []

    async def fake_post(payload):
        posted.append(payload)
        return {"ok": True, "spoken_summary": "Playing Dune on Netflix.", "needs_confirm": None}

    monkeypatch.setattr("stackchan_mcp.roku_play.post_play", fake_post)
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=AsyncMock(),
        agent_id="agent",
    )
    names = {item["name"] for item in bridge._configure_tools({"tools": []})}
    assert "roku_play" in names
    assert "device_command" in names
    assert ROKU_INSTRUCTIONS in bridge._provider_tool_instructions()
    bridge._send = AsyncMock()
    bridge.running = True
    await bridge._tool(
        {
            "name": "roku_play",
            "call_id": "r",
            "arguments": json.dumps({"title": "Dune", "service": "Netflix"}),
        }
    )
    assert posted == [{"title": "Dune", "service": "Netflix"}]
