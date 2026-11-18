# SPDX-License-Identifier: MIT
"""Grok decides every Portal command. These tests cover the gateway half."""

import json

import pytest

from stackchan_mcp.grok_state import GrokProgress, grok_state_frame
from stackchan_mcp.koji_voice import store_koji_context
from stackchan_mcp.portal_skill import portal_skill_block
from stackchan_mcp.portal_state import accept_status, mark_stale, parse_volume
from stackchan_mcp.quiet_context import QUIET_PREFIX, QuietContext


def test_grok_state_orders_one_heard_per_turn():
    progress = GrokProgress()
    assert progress.on_user_audio_boundary() == "heard"
    assert progress.on_user_audio_boundary() is None
    assert progress.on_response_created() == "working"
    assert progress.on_tool() == "tool"
    assert progress.on_response_done() is None
    assert progress.on_audio_ended() == "done"
    progress.on_speech_started()
    assert progress.on_user_audio_boundary() == "heard"
    assert grok_state_frame("working", 4) == {"type": "grok_state", "seq": 4, "state": "working"}


@pytest.mark.asyncio
async def test_grok_state_is_not_sent_for_a_quiet_device():
    from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge

    sent = []

    class Device:
        one_voice = False

        async def send_grok_state(self, frame):
            sent.append(frame)

    bridge = GbotVoiceBridge(
        on_audio=lambda _pcm: None,
        on_turn_complete=lambda: None,
        on_interrupt=lambda: None,
        on_session_dead=lambda: None,
        esp32=Device(),
        local_tools=False,
    )
    await bridge._handle_event({"type": "input_audio_buffer.speech_stopped"})
    await bridge._handle_event({"type": "response.created", "response": {"id": "r"}})
    assert sent == []


def test_podcast_followed_rows_are_title_and_subtitle_in_router_order():
    from stackchan_mcp.podcast_followed import followed_rows

    rows = followed_rows(
        [
            {"title": "Hard Fork", "subtitle": "Tech"},
            {"title": "All-In", "author": "Business"},
        ]
    )
    assert rows == [
        {"title": "All-In", "subtitle": "Business"},
        {"title": "Hard Fork", "subtitle": "Tech"},
    ]
    block = portal_skill_block()
    assert "call podcast_followed" in block


def test_skill_block_has_the_judgment_rules_and_the_seven_examples():
    text = portal_skill_block()
    for phrase in (
        "You are the only voice",
        "The Portal does not speak",
        "Before you answer, decide whether the request needs a Portal action.",
        "Questions about the world never call a Portal tool.",
        "call get_portal_state",
        "If an action is needed, call the tool first.",
        "Do not say 'done', 'command sent' or tool names.",
        "If koji_context.reflex shows the Portal already ducked or cancelled something, do not repeat it.",
        "If ok is false or confirmed is false, say plainly what happened in one line and offer one alternative.",
        "Never state a Portal fact that is not in the latest portal_state.",
        "portal_state.panel tells you whether a panel is open",
        "If an action is in_progress, do not issue it again; say it's on its way.",
        "If portal_state is stale, say so before relying on it.",
        "Put on the latest Hard Fork.",
        "Too loud.",
        "What's playing?",
        "Who founded Nvidia?",
        "Nothing's playing on the Portal right now.",
        "Turn the Mac volume down.",
        "Paused.",
    ):
        assert phrase in text
    assert "media_control" not in text
    assert "Mac set_volume" not in text
    assert "send_task" in text


def test_koji_context_keeps_known_reflexes_and_drops_the_rest():
    stored = store_koji_context(
        {
            "v": 1,
            "seq": 3,
            "reflex": ["ducked", "nope", "grok_cut"],
            "offline_actions": ["media_pause", "play the show"],
            "portal_events": [{"type": "playback_paused", "rev": 4, "show": "Hard Fork"}],
        }
    )
    assert stored["reflex"] == ["ducked", "grok_cut"]
    assert stored["offline_actions"] == ["media_pause"]
    assert stored["portal_events"] == [{"type": "playback_paused", "rev": 4}]
    assert "Hard Fork" not in json.dumps(stored["portal_events"])


def test_a_lower_rev_does_not_replace_the_cache_and_a_timeout_is_stale():
    first = {"rev": 5, "epoch": "a", "media": {"where": "spotify"}}
    assert accept_status(None, first)["rev"] == 5
    assert accept_status(first, {"rev": 4, "epoch": "a"}) is first
    assert accept_status(first, {"rev": 6, "epoch": "b"})["rev"] == 6
    stale = mark_stale(first)
    assert stale["stale"] is True
    assert parse_volume("level 4 max 15 percent 26") == {"step": 4, "max": 15, "percent": 26}


def test_quiet_context_coalesces_defers_and_rate_limits():
    quiet = QuietContext()
    quiet.offer({"rev": 1}, [{"type": "reflex", "rev": 1}])
    quiet.offer({"rev": 2}, [{"type": "reflex", "rev": 2}, {"type": "playback_paused", "rev": 3}])
    assert quiet.due(0, response_active=True) is None
    item = quiet.due(10, response_active=False)
    assert item["type"] == "conversation.item.create"
    assert item["item"]["role"] == "system"
    assert "response.create" not in json.dumps(item)
    quiet.note_sent()
    quiet.offer({"rev": 3}, [{"type": "bridge_up", "rev": 3}])
    assert quiet.due(11, response_active=False) is None
    assert quiet.due(12.1, response_active=False)["item"]["role"] == "system"


def test_quiet_context_falls_back_to_a_user_item_and_keeps_events():
    quiet = QuietContext()
    quiet.offer({"rev": 1}, [{"type": "action_final", "action_id": "cmd-1"}])
    first = quiet.due(5, response_active=False)
    assert first["item"]["role"] == "system"
    retry = quiet.note_rejected()
    assert retry["item"]["role"] == "user"
    assert retry["item"]["content"][0]["text"].startswith(QUIET_PREFIX)
    quiet.fail()
    assert quiet.take_attached()[0]["action_id"] == "cmd-1"


@pytest.mark.asyncio
async def test_inject_context_quiet_never_requests_a_response():
    from unittest.mock import AsyncMock

    from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge

    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        local_tools=False,
    )
    bridge._send = AsyncMock()
    bridge.running = True
    await bridge.inject_context_quiet("volume changed")
    kinds = [call.args[0]["type"] for call in bridge._send.await_args_list]
    assert kinds == ["conversation.item.create"]
    assert bridge._send.await_args_list[0].args[0]["item"]["role"] == "system"
    bridge._response_in_progress = True
    await bridge.inject_context_quiet("later")
    assert [call.args[0]["type"] for call in bridge._send.await_args_list] == ["conversation.item.create"]
    await bridge.stop()


@pytest.mark.asyncio
async def test_one_voice_get_volume_waits_and_a_timeout_is_unconfirmed(monkeypatch):
    from stackchan_mcp.device_commands import device_command_extension

    class Device:
        one_voice = True
        koji_context = None
        pending_portal_events = []

        async def send_device_command(self, frame, turn_seq=None):
            assert frame["name"] == "get_volume"
            return {
                "ok": True,
                "confirmed": True,
                "detail": "level 4 max 15 percent 26",
                "portal_state": {"rev": 8, "media": {"where": "none"}},
                "action_id": "cmd-3",
                "state": "done",
            }, None

    result = await device_command_extension(Device()).dispatch("device_command", {"name": "get_volume", "args": {}})
    assert result["step"] == 4
    assert result["max"] == 15
    assert result["percent"] == 26
    assert result["portal_state"]["rev"] == 8
    assert result["action_id"] == "cmd-3"

    class Slow:
        one_voice = True
        koji_context = None
        pending_portal_events = [{"type": "offline_command", "rev": 1}]

        async def send_device_command(self, frame, turn_seq=None):
            return {"ok": True, "confirmed": False, "portal_state": {"rev": 2, "stale": True}}, None

    missed = await device_command_extension(Slow()).dispatch("device_command", {"name": "media_pause", "args": {}})
    assert missed["confirmed"] is False
    assert missed["portal_state"]["stale"] is True
    assert missed["portal_events"][0]["type"] == "offline_command"


def test_declared_aliases_are_one_name_each():
    from stackchan_mcp.core_frames import CORE_DECLARED_NAMES
    from stackchan_mcp.panel_frames import PANEL_DECLARED_NAMES

    assert CORE_DECLARED_NAMES == ("core_morph", "core_say")
    assert PANEL_DECLARED_NAMES == ("show_panel", "dismiss_panel")
    from stackchan_mcp.core_frames import core_tool_description

    assert "makes no sound" in core_tool_description("core_say")
