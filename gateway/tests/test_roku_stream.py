# SPDX-License-Identifier: MIT
"""roku_stream posts one router request and does not touch the Mac."""

import json
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge
from stackchan_mcp.roku_stream import (
    ROKU_STREAM_INSTRUCTIONS,
    STOP_POST_TIMEOUT_S,
    STREAM_POST_TIMEOUT_S,
    post_stop,
    post_stream,
    roku_stream_enabled,
    roku_stream_extension,
    stream_payload,
)


def test_instructions_send_the_browser_and_leave_titles_to_roku_play():
    text = ROKU_STREAM_INSTRUCTIONS
    assert "roku_stream" in text
    assert "roku_play" in text
    assert "spoken_summary" in text
    assert "Putting the game on the TV." in text
    assert "ok is false" in text
    assert "browser" in text
    assert "link" in text
    assert "Do not play the video on the Mac" in text
    assert "projector" in text


def test_payload_is_the_tab_or_a_link():
    assert stream_payload({"source": "chrome"}) == {"source": "chrome"}
    assert stream_payload({"source": "chrome", "title": " Final "}) == {
        "source": "chrome",
        "title": "Final",
    }
    assert stream_payload({"source": "url", "url": " https://example.com/a ", "title": "Game"}) == {
        "source": "url",
        "url": "https://example.com/a",
        "title": "Game",
    }
    assert stream_payload({"source": "url"}) is None
    assert stream_payload({"source": "app", "url": "https://example.com"}) is None
    assert stream_payload({}) is None


def test_the_tool_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("STACKCHAN_ROKU_STREAM", "0")
    assert roku_stream_enabled() is False
    monkeypatch.setenv("STACKCHAN_ROKU_STREAM", "on")
    assert roku_stream_enabled() is True


def test_timeouts_leave_room_for_a_slow_lookup():
    assert STREAM_POST_TIMEOUT_S == 25
    assert STOP_POST_TIMEOUT_S == 8
    extension = roku_stream_extension()
    assert extension.metadata["roku_stream"].timeout_s == 30
    assert extension.metadata["roku_stream_stop"].timeout_s == 12


@pytest.mark.asyncio
async def test_extension_posts_the_stream_and_says_the_router_line(monkeypatch):
    posted = []

    async def fake_post(payload):
        posted.append(payload)
        return {"ok": True, "spoken_summary": "Putting the game on the TV."}

    async def fake_stop():
        posted.append("stop")
        return {"ok": True, "spoken_summary": "Taking it off the TV."}

    monkeypatch.setattr("stackchan_mcp.roku_stream.post_stream", fake_post)
    monkeypatch.setattr("stackchan_mcp.roku_stream.post_stop", fake_stop)
    extension = roku_stream_extension()
    assert [entry["name"] for entry in extension.declarations] == ["roku_stream", "roku_stream_stop"]
    result = await extension.dispatch("roku_stream", {"source": "chrome"})
    assert result["spoken_summary"] == "Putting the game on the TV."
    link = await extension.dispatch(
        "roku_stream",
        {"source": "url", "url": "https://example.com/a", "title": "Game"},
    )
    assert link["ok"] is True
    stopped = await extension.dispatch("roku_stream_stop", {})
    assert stopped["spoken_summary"] == "Taking it off the TV."
    assert posted == [
        {"source": "chrome"},
        {"source": "url", "url": "https://example.com/a", "title": "Game"},
        "stop",
    ]
    missing = await extension.dispatch("roku_stream", {})
    assert missing == {"ok": False, "error": "source is required"}
    no_url = await extension.dispatch("roku_stream", {"source": "url"})
    assert no_url == {"ok": False, "error": "url is required"}
    other = await extension.dispatch("roku_play", {"title": "Dune"})
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
    result = await post_stream({"source": "chrome"})
    assert result["ok"] is False
    assert result["spoken_summary"] == "I can't reach the TV."
    assert "down" not in json.dumps(result)
    stopped = await post_stop()
    assert stopped["spoken_summary"] == "I can't reach the TV."


@pytest.mark.asyncio
async def test_posts_use_a_long_lookup_and_a_short_stop(monkeypatch):
    seen = []

    class Response:
        status = 200

        async def json(self, content_type=None):
            return {"ok": False, "spoken_summary": "That stream is DRM-protected, so I can't send it to the TV."}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None):
            seen.append((url, self.timeout.total))
            return Response()

    monkeypatch.setattr("aiohttp.ClientSession", Session)
    result = await post_stream({"source": "url", "url": "https://example.com/a"})
    assert result["spoken_summary"].startswith("That stream is DRM-protected")
    await post_stop()
    assert seen[0][0].endswith("/api/roku/stream")
    assert seen[0][1] == 25
    assert seen[1][0].endswith("/api/roku/stream/stop")
    assert seen[1][1] == 8


@pytest.mark.asyncio
async def test_gbot_voice_declares_roku_stream(monkeypatch):
    monkeypatch.delenv("STACKCHAN_TOOL_BOT", raising=False)
    monkeypatch.delenv("STACKCHAN_TOOL_BOT_ID", raising=False)
    monkeypatch.delenv("STACKCHAN_ROKU_STREAM", raising=False)
    posted = []

    async def fake_post(payload):
        posted.append(payload)
        return {"ok": True, "spoken_summary": "Putting the game on the TV."}

    monkeypatch.setattr("stackchan_mcp.roku_stream.post_stream", fake_post)
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=AsyncMock(),
        agent_id="agent",
    )
    names = {item["name"] for item in bridge._configure_tools({"tools": []})}
    assert "roku_stream" in names
    assert "roku_stream_stop" in names
    assert "roku_play" in names
    assert ROKU_STREAM_INSTRUCTIONS in bridge._provider_tool_instructions()
    bridge._send = AsyncMock()
    bridge.running = True
    await bridge._tool(
        {
            "name": "roku_stream",
            "call_id": "s",
            "arguments": json.dumps({"source": "url", "url": "https://example.com/game"}),
        }
    )
    assert posted == [{"source": "url", "url": "https://example.com/game"}]
