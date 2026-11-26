# SPDX-License-Identifier: MIT
"""Grok decides every Portal command. These tests cover the gateway half."""

import asyncio
import json

import pytest

from stackchan_mcp.grok_state import GrokProgress, grok_state_frame
from stackchan_mcp.koji_voice import store_koji_context
from stackchan_mcp.portal_skill import portal_skill_block
from stackchan_mcp.portal_state import accept_status, mark_stale, parse_volume
from stackchan_mcp.quiet_context import QUIET_PREFIX, QuietContext


def test_one_voice_instructions_drop_portal_speech(monkeypatch):
    from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge
    from stackchan_mcp.panel_frames import PANEL_INSTRUCTIONS, panel_frame_extension
    from stackchan_mcp.podcast_follow import FOLLOW_INSTRUCTIONS, podcast_follow_extension
    from stackchan_mcp.podcast_open import PODCAST_INSTRUCTIONS, podcast_open_extension

    class Esp:
        one_voice = True

    assert podcast_open_extension(Esp()).instructions_for() == ""
    assert podcast_follow_extension(Esp()).instructions_for() == ""
    assert panel_frame_extension(Esp()).instructions_for() == ""

    class Quiet:
        one_voice = False

    assert podcast_open_extension(Quiet()).instructions_for() == PODCAST_INSTRUCTIONS
    assert podcast_follow_extension(Quiet()).instructions_for() == FOLLOW_INSTRUCTIONS
    assert panel_frame_extension(Quiet()).instructions_for() == PANEL_INSTRUCTIONS

    bridge = GbotVoiceBridge(
        on_audio=lambda _pcm: None,
        on_turn_complete=lambda: None,
        on_interrupt=lambda: None,
        on_session_dead=lambda: None,
        esp32=Esp(),
        local_tools=False,
    )
    bridge._executor = type("Exec", (), {"instructions": lambda self, model_search=False: ""})()
    bridge._local_tools_enabled = True
    bridge.metrics["declared_tools"] = ["device_command", "podcast_open_spotify", "show_panel"]
    text = bridge._provider_tool_instructions()
    assert "The Portal speaks" not in text
    assert "Do not say anything before or after" not in text
    assert "self.panel" not in text
    assert "ordinal" in text
    assert "did_you_mean" in text
    assert "one_open_per_turn" in text


def test_a_new_epoch_is_accepted_when_it_is_newer():
    old = {"rev": 9, "epoch": "old", "at_ms": 1000}
    assert accept_status(old, {"rev": 1, "epoch": "new", "at_ms": 500}) is old
    assert accept_status(old, {"rev": 1, "epoch": "new", "at_ms": 2000})["epoch"] == "new"
    assert accept_status(old, {"rev": 1, "epoch": "new", "at_ms": 500}, fresh_session=True)["rev"] == 1


def test_panel_match_confirms_show_and_dismiss():
    from stackchan_mcp.portal_state import panel_matches

    shown = {"panel": {"current": {"id": "p1"}}}
    assert panel_matches(shown, show_id="p1", dismiss_id=None) is True
    assert panel_matches(shown, show_id="other", dismiss_id=None) is False
    closed = {"panel": {"last_closed": {"id": "p1"}}}
    assert panel_matches(closed, show_id=None, dismiss_id="p1") is True
    assert panel_matches({}, show_id="p1", dismiss_id=None) is False


def test_declared_tools_are_the_only_ones_listed():
    from stackchan_mcp.portal_skill import portal_skill_block

    block = portal_skill_block(declared=("device_command", "show_panel"))
    assert "Portal tools: device_command, show_panel." in block
    assert "roku_play" not in block.split("Portal tools:")[1]


@pytest.mark.asyncio
async def test_flush_quiet_resends_a_rejected_system_item_as_user():
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
    bridge._quiet.offer({"rev": 1, "media": {"where": "spotify"}}, [{"type": "reflex", "what": "ducked"}])
    assert await bridge._flush_quiet() is True
    assert bridge._send.await_args_list[0].args[0]["item"]["role"] == "system"
    await bridge._handle_event({"type": "error", "error": {"code": "invalid_request_error", "param": "item"}})
    await asyncio.sleep(0)
    roles = [call.args[0]["item"]["role"] for call in bridge._send.await_args_list]
    kinds = [call.args[0]["type"] for call in bridge._send.await_args_list]
    assert roles == ["system", "user"]
    assert "response.create" not in kinds
    user = bridge._send.await_args_list[1].args[0]
    assert user["item"]["id"].startswith("quiet_")
    assert user["item"]["role"] == "user"
    assert "Hard Fork" not in json.dumps(user)
    await bridge._handle_event({"type": "conversation.item.added", "item": {"id": "other"}})
    assert bridge._quiet_outstanding is True
    await bridge._handle_event({"type": "conversation.item.added", "item": {"id": user["item"]["id"]}})
    assert bridge._quiet.events == []
    assert await bridge._flush_quiet() is False
    await bridge.stop()


@pytest.mark.asyncio
async def test_events_are_not_both_injected_and_attached():
    from unittest.mock import AsyncMock

    from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge

    class Device:
        one_voice = True
        pending_portal_events = []
        quiet = None

    device = Device()
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=device,
        local_tools=False,
    )
    from stackchan_mcp.quiet_context import QuietContext

    device.quiet = QuietContext()
    bridge._send = AsyncMock()
    device.quiet.offer({"rev": 2}, [{"type": "playback_paused", "rev": 2}])
    assert await bridge._flush_quiet() is True
    assert device.pending_portal_events == []
    sent = bridge._send.await_args_list[0].args[0]
    await bridge._handle_event({"type": "conversation.item.added", "item": {"id": "user_audio_1"}})
    assert device.quiet.events
    assert bridge._quiet_outstanding is True
    await bridge._handle_event({"type": "conversation.item.added", "item": {"id": sent["item"]["id"]}})
    assert device.quiet.events == []
    assert device.pending_portal_events == []
    await bridge.stop()


@pytest.mark.asyncio
async def test_get_portal_state_from_a_live_portal_is_not_stale():
    from stackchan_mcp.portal_read import PortalStatusCache, read_portal_state

    class Esp:
        device_connected = True

        async def send_device_command(self, _frame):
            return {"ok": True, "portal_state": {"rev": 4, "epoch": "e", "media": {"where": "spotify"}}}, None

    view = await read_portal_state(Esp(), PortalStatusCache())
    assert view["stale"] is False
    assert view["source"] == "portal"


def test_grok_state_orders_one_heard_per_turn():
    progress = GrokProgress()
    assert progress.on_user_audio_boundary() == "heard"
    progress.on_speech_started()
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


def test_a_snapshot_alone_is_not_an_item():
    quiet = QuietContext()
    quiet.offer({"rev": 4, "media": {"where": "spotify", "show": "Hard Fork"}}, [])
    assert quiet.due(10, response_active=False) is None
    quiet.offer({"rev": 4}, [{"type": "action_final", "action_id": "cmd-1", "state": "done"}])
    item = quiet.due(10, response_active=False)
    assert item["item"]["id"].startswith("quiet_")
    assert item["item"]["role"] == "system"
    assert "Hard Fork" not in item["item"]["id"]


def test_heartbeat_frame_updates_the_cache_and_injects_nothing():
    from stackchan_mcp.esp32_client import ESP32Manager

    device = ESP32Manager()
    calls = []
    device.on_portal_frame = lambda: calls.append(1)
    device._take_portal_frame({"type": "portal_status", "status": {"rev": 1, "epoch": "e", "media": {"show": "Hard Fork"}}})
    assert device.quiet.events == []
    assert calls == []
    assert device.portal_cache.status["rev"] == 1
    device._take_portal_frame({
        "type": "action_final",
        "action": {"action_id": "cmd-9", "state": "done"},
        "portal_state": {"rev": 2, "epoch": "e"},
    })
    assert len(device.quiet.events) == 1
    assert device.quiet.events[0]["type"] == "action_final"
    assert calls == [1]


@pytest.mark.asyncio
async def test_no_injection_between_speech_started_and_response_done():
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
    bridge._quiet.offer({"rev": 1}, [{"type": "action_final", "action_id": "cmd-1", "state": "done"}])
    await bridge._handle_event({"type": "input_audio_buffer.speech_started"})
    await bridge._handle_event({"type": "input_audio_buffer.speech_stopped"})
    assert await bridge._flush_quiet() is False
    assert bridge._send.await_count == 0
    await bridge._finish_turn()
    assert bridge._send.await_count == 1
    assert bridge._send.await_args.args[0]["item"]["role"] == "system"
    await bridge.stop()


def test_device_hello_and_expiry_clear_a_stuck_turn():
    from stackchan_mcp.esp32_client import ESP32Manager

    device = ESP32Manager()
    device.quiet.hold_turn(0)
    device.note_device_hello()
    assert device.quiet.turn_pending is False
    device.quiet.hold_turn(0)
    device.quiet.offer({"rev": 1}, [{"type": "action_final", "action_id": "cmd-1", "state": "done"}])
    assert device.quiet.expire_turn(44) is False
    assert device.quiet.due(44, response_active=False) is None
    assert device.quiet.expire_turn(45) is True
    item = device.quiet.due(45, response_active=False)
    assert item["item"]["role"] == "system"


@pytest.mark.asyncio
async def test_accept_timeout_ignores_a_replaced_item_id():
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
    bridge._quiet.offer({"rev": 1}, [{"type": "action_final", "action_id": "cmd-1", "state": "done"}])
    assert await bridge._flush_quiet() is True
    system_id = bridge._quiet_id
    await bridge._handle_event({"type": "error", "error": {"code": "invalid_request_error", "param": "item"}})
    await asyncio.sleep(0)
    retry_id = bridge._quiet_id
    assert retry_id != system_id
    await bridge._quiet_accept_timeout(system_id, delay=0)
    assert bridge._quiet_outstanding is True
    assert bridge._quiet.events
    await bridge._handle_event({"type": "conversation.item.added", "item": {"id": retry_id}})
    assert bridge._quiet.events == []
    await bridge.stop()


@pytest.mark.asyncio
async def test_a_session_error_and_stop_clear_a_stuck_turn():
    from unittest.mock import AsyncMock

    from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge

    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        local_tools=False,
    )
    bridge._quiet.hold_turn(0)
    await bridge._handle_event({"type": "response.done", "response": {"id": "other"}})
    assert bridge._quiet.turn_pending is False
    bridge._quiet.hold_turn(1)
    await bridge._handle_event({"type": "error", "error": {"code": "session_expired", "param": "session"}})
    assert bridge._quiet.turn_pending is False
    bridge._quiet.hold_turn(2)
    await bridge.stop()
    assert bridge._quiet.turn_pending is False


def test_conversation_end_event_reaches_grok_without_the_phrase():
    from stackchan_mcp.portal_state import clean_events

    kept = clean_events(
        [
            {
                "type": "conversation",
                "action_id": "active",
                "delta": {"result": "end_phrase", "show": "that's all"},
            }
        ]
    )
    assert kept == [
        {
            "type": "conversation",
            "action_id": "active",
            "delta": {"result": "end_phrase"},
        }
    ]


def _bridge(device=None):
    from unittest.mock import AsyncMock

    from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge

    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=device,
        local_tools=False,
    )
    bridge._send = AsyncMock()
    return bridge


@pytest.mark.asyncio
async def test_item_added_accepts_by_id_and_logs_the_shape(caplog):
    import logging

    bridge = _bridge()
    bridge._quiet.offer({"rev": 1}, [{"type": "reflex", "what": "ducked"}])
    assert await bridge._flush_quiet() is True
    sent = bridge._send.await_args_list[0].args[0]["item"]
    with caplog.at_level(logging.INFO):
        await bridge._handle_event(
            {
                "type": "conversation.item.added",
                "event_id": "evt_1",
                "previous_item_id": None,
                "item": {"id": sent["id"], "type": "message", "role": "system"},
            }
        )
    assert bridge._quiet.events == []
    assert bridge._quiet_outstanding is False
    assert "portal_event accepted=true match=id" in caplog.text
    assert "item_added keys=" in caplog.text
    assert "id_shape=client" in caplog.text
    assert "ducked" not in caplog.text
    bridge._quiet._last_sent = -1_000.0
    bridge._quiet.offer({"rev": 2}, [{"type": "reflex", "what": "alias"}])
    assert await bridge._flush_quiet() is True
    alias = bridge._send.await_args_list[-1].args[0]["item"]
    await bridge._handle_event({"type": "conversation.item.created", "item": {"id": alias["id"]}})
    assert bridge._quiet.events == []
    await bridge.stop()


@pytest.mark.asyncio
async def test_item_added_accepts_by_content_when_the_id_differs(caplog):
    import logging

    bridge = _bridge()
    bridge._quiet.offer({"rev": 1}, [{"type": "reflex", "what": "UniqueShowZZZ"}])
    assert await bridge._flush_quiet() is True
    sent = bridge._send.await_args_list[0].args[0]["item"]
    text = sent["content"][0]["text"]
    with caplog.at_level(logging.INFO):
        await bridge._handle_event(
            {
                "type": "conversation.item.added",
                "item": {
                    "id": "item_server_9",
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
    assert bridge._quiet.events == []
    assert "portal_event accepted=true match=content" in caplog.text
    assert "id_shape=server" in caplog.text
    assert "UniqueShowZZZ" not in caplog.text
    await bridge.stop()


@pytest.mark.asyncio
async def test_unrelated_item_added_does_not_accept():
    bridge = _bridge()
    bridge._quiet.offer({"rev": 1}, [{"type": "reflex", "what": "ducked"}])
    assert await bridge._flush_quiet() is True
    await bridge._handle_event(
        {
            "type": "conversation.item.added",
            "item": {
                "id": "audio_1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_audio", "audio": "x"}],
            },
        }
    )
    await bridge._handle_event(
        {
            "type": "conversation.item.added",
            "item": {
                "id": "asst_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Portal status updated. hello"}],
            },
        }
    )
    assert bridge._quiet_outstanding is True
    assert bridge._quiet.events
    await bridge.stop()


@pytest.mark.asyncio
async def test_late_ack_after_timeout_is_not_on_the_next_tool_result():
    from stackchan_mcp.portal_state import attach_portal

    class Device:
        one_voice = True
        pending_portal_events = []

    device = Device()
    bridge = _bridge(device)
    device.quiet = bridge._quiet
    bridge._quiet.offer({"rev": 4}, [{"type": "playback_paused", "rev": 4}])
    assert await bridge._flush_quiet() is True
    item_id = bridge._quiet_id
    batch = list(bridge._quiet.events)
    await bridge._quiet_accept_timeout(item_id, delay=0)
    assert device.pending_portal_events == []
    assert bridge._quiet.events == []
    device.pending_portal_events = list(batch)
    await bridge._handle_event({"type": "conversation.item.added", "item": {"id": item_id}})
    assert device.pending_portal_events == []
    delivered = attach_portal(device, {"ok": True})
    assert "portal_events" not in delivered
    await bridge.stop()


@pytest.mark.asyncio
async def test_error_for_the_outstanding_item_retries_then_attaches_once():
    from stackchan_mcp.portal_state import attach_portal

    class Device:
        one_voice = True
        pending_portal_events = []

    device = Device()
    bridge = _bridge(device)
    device.quiet = bridge._quiet
    bridge._quiet.offer({"rev": 1}, [{"type": "action_final", "action_id": "cmd-9"}])
    assert await bridge._flush_quiet() is True
    await bridge._handle_event(
        {"type": "error", "error": {"code": "invalid_request_error", "param": "item"}}
    )
    await asyncio.sleep(0)
    assert device.pending_portal_events == []
    roles = [call.args[0]["item"]["role"] for call in bridge._send.await_args_list]
    assert roles == ["system", "user"]
    await bridge._handle_event(
        {"type": "error", "error": {"code": "invalid_request_error", "param": "item"}}
    )
    await asyncio.sleep(0)
    assert [call.args[0]["type"] for call in bridge._send.await_args_list].count("conversation.item.create") == 2
    assert device.pending_portal_events == [{"type": "action_final", "action_id": "cmd-9"}]
    once = attach_portal(device, {"ok": True})
    assert once["portal_events"] == [{"type": "action_final", "action_id": "cmd-9"}]
    again = attach_portal(device, {"ok": True})
    assert "portal_events" not in again
    await bridge.stop()


@pytest.mark.asyncio
async def test_user_audio_item_added_does_not_kill_the_session(caplog):
    import logging

    bridge = _bridge()
    user_audio = {
        "type": "conversation.item.added",
        "item": {"id": "item_abc", "type": "message", "role": "user"},
    }
    with caplog.at_level(logging.INFO):
        await bridge._handle_event(user_audio)
    await bridge._handle_event({"type": "session.updated"})
    assert bridge._connected_event.is_set()
    assert "id_shape=" not in caplog.text
    bridge._item_added_seen = True
    bridge._local = __import__("unittest").mock.AsyncMock(side_effect=RuntimeError("stop"))
    with pytest.raises(RuntimeError, match="details redacted"):
        await bridge.start()
    assert bridge._item_added_seen is False
    await bridge._handle_event(user_audio)
    bridge._connected_event.clear()
    await bridge._handle_event({"type": "session.updated"})
    assert bridge._connected_event.is_set()
    await bridge.stop()


@pytest.mark.asyncio
async def test_interrupt_during_the_note_wait_sends_the_batch_once():
    bridge = _bridge()
    bridge._quiet.offer({"rev": 1}, [{"type": "reflex", "rev": 1}])
    assert await bridge._flush_quiet() is True
    item_id = bridge._quiet_id
    await bridge.interrupt()
    bridge._quiet._last_sent = -1_000.0
    assert await bridge._flush_quiet() is False
    creates = [
        call.args[0]
        for call in bridge._send.await_args_list
        if isinstance(call.args[0], dict) and call.args[0].get("type") == "conversation.item.create"
    ]
    assert len(creates) == 1
    await bridge._handle_event(
        {"type": "conversation.item.added", "item": {"id": item_id, "type": "message", "role": "system"}}
    )
    creates = [
        call.args[0]
        for call in bridge._send.await_args_list
        if isinstance(call.args[0], dict) and call.args[0].get("type") == "conversation.item.create"
    ]
    assert len(creates) == 1
    assert bridge._quiet_outstanding is False
    await bridge.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["ack", "timeout", "interrupt"])
async def test_events_offered_during_the_wait_stay_queued(how):
    import json

    bridge = _bridge()
    older = {"type": "reflex", "rev": 1}
    newer = {"type": "reflex", "rev": 2}
    bridge._quiet.offer({"rev": 1}, [older])
    assert await bridge._flush_quiet() is True
    bridge._quiet.offer({"rev": 2}, [newer])
    if how == "ack":
        await bridge._handle_event(
            {
                "type": "conversation.item.added",
                "item": {"id": bridge._quiet_id, "type": "message", "role": "system"},
            }
        )
    elif how == "timeout":
        await bridge._quiet_accept_timeout(bridge._quiet_id, delay=0)
    else:
        await bridge.interrupt()
    assert older not in bridge._quiet.events
    assert bridge._quiet.events == [newer]
    bridge._quiet._last_sent = -1_000.0
    assert await bridge._flush_quiet() is True
    text = bridge._send.await_args_list[-1].args[0]["item"]["content"][0]["text"]
    body = json.loads(text.split("Portal status updated. ", 1)[1])
    assert body["portal_events"] == [newer]
    await bridge.stop()
