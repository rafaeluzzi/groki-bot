# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Portal type:panel documents and the gbot voice forwarder."""

import json
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge
from stackchan_mcp.panel_frames import (
    PANEL_DECLARED_NAMES,
    PANEL_INSTRUCTIONS,
    build_panel_frame,
    is_panel_frame,
    panel_action_for_tool,
    panel_frame_extension,
    panel_frames_enabled,
)

LIST_DOC = {
    "version": 1,
    "kind": "list",
    "title": "Queue",
    "items": [
        {"id": "a", "title": "First", "subtitle": "Ada"},
        {"label": "Second"},
    ],
}
PLAYING_DOC = {
    "version": 1,
    "kind": "now_playing",
    "title": "Night",
    "artist": "Ada",
    "progress": 0.25,
    "paused": False,
}
CHOICE_DOC = {
    "version": 1,
    "kind": "choice",
    "prompt": "Tea or coffee?",
    "options": [{"id": "tea", "label": "Tea"}, {"title": "Coffee"}],
}
WEBVIEW_DOC = {
    "version": 1,
    "kind": "webview",
    "html": "<p>Hello</p>",
    "title": "Note",
}


@pytest.mark.parametrize(
    "name,action",
    [
        ("show_panel", "show"),
        ("panel_show", "show"),
        ("self.panel.show", "show"),
        ("self_panel_show", "show"),
        ("SHOW.PANEL", "show"),
        ("dismiss_panel", "dismiss"),
        ("panel_dismiss", "dismiss"),
        ("self.panel.dismiss", "dismiss"),
    ],
)
def test_panel_tool_names_map_to_one_action(name, action):
    assert panel_action_for_tool(name) == action


@pytest.mark.parametrize(
    "name",
    ["", "panel", "core_morph", "core_say", "core_draw", "draw", "self.core.morph", "show"],
)
def test_unrelated_tool_names_are_not_panel_actions(name):
    assert panel_action_for_tool(name) is None


def test_panel_instructions_name_the_screen_rules():
    text = PANEL_INSTRUCTIONS
    assert "beside the orb" in text
    assert "two thirds" in text
    assert "kind list, now_playing, or choice" in text
    assert "show_panel" in text
    assert "dismiss_panel" in text
    assert "self.panel.show" in text
    assert "Panels do not replace your spoken reply" in text
    assert "before the tool returns ok" in text
    assert "open-ended follow-up" in text
    assert "webview or html" in text
    assert "core_draw" not in text
    assert "draw" not in text.lower()


def test_panel_frames_match_the_portal_shape():
    assert build_panel_frame("show_panel", {"panel": LIST_DOC}) == {
        "type": "panel",
        "panel": LIST_DOC,
    }
    assert build_panel_frame("self.panel.show", {"panel": json.dumps(PLAYING_DOC)}) == {
        "type": "panel",
        "panel": PLAYING_DOC,
    }
    choice = build_panel_frame("panel_show", {"panel": CHOICE_DOC})
    assert choice["panel"]["kind"] == "choice"
    assert choice["panel"]["options"][1] == {"title": "Coffee"}
    webview = build_panel_frame("show_panel", {"panel": WEBVIEW_DOC})
    assert webview["panel"]["html"] == "<p>Hello</p>"
    assert webview["panel"]["title"] == "Note"
    html = build_panel_frame(
        "show_panel",
        {"panel": {"version": 1, "kind": "html", "url": "https://example.com/card"}},
    )
    assert html == {
        "type": "panel",
        "panel": {"version": 1, "kind": "html", "url": "https://example.com/card"},
    }
    markup = {"version": 1, "kind": "webview", "page": {"markup": "<p>Later</p>"}}
    assert build_panel_frame("show_panel", {"panel": markup})["panel"] == markup
    assert build_panel_frame("dismiss_panel", {}) == {"type": "panel", "dismiss": True}
    assert build_panel_frame("self.panel.dismiss", {})["dismiss"] is True
    assert is_panel_frame(build_panel_frame("show_panel", {"panel": LIST_DOC}))
    assert is_panel_frame({"type": "panel", "dismiss": True})
    assert not is_panel_frame({**webview, "session_id": "extra"})


@pytest.mark.parametrize(
    "panel",
    [
        {"version": 1, "kind": "banner"},
        {"version": 1, "kind": "draw", "items": [{"title": "Hi"}]},
        {"version": 2, "kind": "list", "items": [{"title": "Hi"}]},
        {"version": "1", "kind": "list", "items": [{"title": "Hi"}]},
        {"kind": "list", "items": [{"title": "Hi"}]},
        {"version": 1, "kind": "list"},
        {"version": 1, "kind": "list", "items": []},
        {"version": 1, "kind": "list", "items": ["Hi"]},
        {"version": 1, "kind": "list", "items": [{"title": "  "}]},
        {"version": 1, "kind": "list", "items": [{"title": "bad\x00"}]},
        {"version": 1, "kind": "list", "items": [{"title": "bad\x01"}]},
        {"version": 1, "kind": "now_playing"},
        {"version": 1, "kind": "now_playing", "title": "Song", "progress": 2},
        {"version": 1, "kind": "choice", "prompt": "Pick"},
        {"version": 1, "kind": "choice", "options": [{"label": "Yes"}]},
        {"version": 1, "kind": "webview"},
        {"version": 1, "kind": "webview", "count": 1},
        {"version": 1, "kind": "html", "html": ""},
        {"version": 1, "kind": "html", "html": "x" * 40000},
        [],
        "not json",
        "",
    ],
)
def test_bad_panel_documents_are_rejected(panel):
    assert build_panel_frame("show_panel", {"panel": panel}) is None
    assert build_panel_frame("show_panel", {"panel": json.dumps(panel)}) is None


def test_dismiss_rejects_a_document_and_show_rejects_extra_arguments():
    assert build_panel_frame("dismiss_panel", {"panel": LIST_DOC}) is None
    assert build_panel_frame("show_panel", {"panel": LIST_DOC, "kind": "list"}) is None
    assert build_panel_frame("show_panel", {}) is None
    assert build_panel_frame("core_draw", {"panel": LIST_DOC}) is None


def test_panel_frames_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("STACKCHAN_PANEL_FRAMES", "0")
    assert panel_frames_enabled() is False
    monkeypatch.setenv("STACKCHAN_PANEL_FRAMES", "on")
    assert panel_frames_enabled() is True


@pytest.mark.asyncio
async def test_extension_sends_one_frame_and_does_not_wait_for_an_ack():
    sent = []

    class Device:
        async def send_panel(self, frame):
            sent.append(frame)
            return {"ok": True}, None

    extension = panel_frame_extension(Device())
    assert [entry["name"] for entry in extension.declarations] == list(PANEL_DECLARED_NAMES)
    result = await extension.dispatch("self.panel.show", {"panel": LIST_DOC})
    assert result == {"ok": True, "kind": "list"}
    dismissed = await extension.dispatch("dismiss_panel", {})
    assert dismissed == {"ok": True, "dismiss": True}
    assert sent == [
        {"type": "panel", "panel": LIST_DOC},
        {"type": "panel", "dismiss": True},
    ]
    rejected = await extension.dispatch("show_panel", {"panel": {"version": 1, "kind": "list"}})
    assert rejected == {"ok": False, "error": "invalid panel arguments"}
    assert len(sent) == 2
    missed = await extension.dispatch("core_draw", {"what": "Hi"})
    assert missed == {"ok": False, "error": "invalid panel arguments"}


@pytest.mark.asyncio
async def test_extension_reports_send_failure_without_the_document():
    class Device:
        async def send_panel(self, frame):
            raise RuntimeError(frame["panel"]["title"])

    result = await panel_frame_extension(Device()).dispatch("show_panel", {"panel": LIST_DOC})
    assert result == {"ok": False, "error": "panel frame failed"}
    assert "Queue" not in json.dumps(result)

    class Offline:
        async def send_panel(self, frame):
            return None, {"code": -32000, "message": "No ESP32 device connected"}

    offline = await panel_frame_extension(Offline()).dispatch("dismiss_panel", {})
    assert offline == {"ok": False, "error": "No ESP32 device connected"}


@pytest.mark.asyncio
async def test_gbot_voice_declares_and_forwards_panel_tools(monkeypatch):
    monkeypatch.delenv("STACKCHAN_TOOL_BOT", raising=False)
    monkeypatch.delenv("STACKCHAN_TOOL_BOT_ID", raising=False)
    monkeypatch.delenv("STACKCHAN_PANEL_FRAMES", raising=False)
    frames = []

    class Device:
        async def send_core(self, frame, timeout_s=3.0):
            frames.append(frame)
            return {"type": "core", "ok": True, "action": frame["action"]}, None

        async def send_panel(self, frame):
            frames.append(frame)
            return {"ok": True}, None

    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=Device(),
        agent_id="agent",
    )
    declarations = bridge._configure_tools(
        {
            "tools": [
                {"name": "unknown_remote_action", "parameters": {"type": "object"}},
                {"name": "panel_show", "parameters": {"type": "object"}},
                {"name": "Self.Core.Draw", "description": "session copy"},
            ]
        }
    )
    names = {item["name"] for item in declarations}
    assert {
        "show_panel",
        "self_panel_show",
        "dismiss_panel",
        "self_panel_dismiss",
        "panel_show",
        "core_morph",
        "core_say",
    } <= names
    for banned in ("core_draw", "draw", "self_core_draw", "Self_Core_Draw", "self.core.draw"):
        assert banned not in names
    assert "unknown_remote_action" not in bridge._remote_tools
    assert "panel_show" not in bridge._remote_tools
    assert bridge._local_tools["panel_show"] == "show_panel"
    injected = bridge._provider_tool_instructions()
    assert PANEL_INSTRUCTIONS in injected
    assert "beside the orb" in injected
    assert "open-ended follow-up" in injected
    assert "core_draw" not in injected

    bridge._send = AsyncMock()
    bridge.running = True
    await bridge._tool(
        {"name": "show_panel", "call_id": "p", "arguments": json.dumps({"panel": LIST_DOC})}
    )
    await bridge._tool(
        {
            "name": "self_panel_show",
            "call_id": "s",
            "arguments": json.dumps({"panel": json.dumps(PLAYING_DOC)}),
        }
    )
    await bridge._tool({"name": "dismiss_panel", "call_id": "d", "arguments": "{}"})
    await bridge._tool(
        {"name": "show_panel", "call_id": "bad", "arguments": json.dumps({"panel": {"kind": "list"}})}
    )
    await bridge._tool({"name": "core_draw", "call_id": "cd", "arguments": '{"what":"Nope"}'})
    assert frames == [
        {"type": "panel", "panel": LIST_DOC},
        {"type": "panel", "panel": PLAYING_DOC},
        {"type": "panel", "dismiss": True},
    ]
    outputs = [
        json.loads(call.args[0]["item"]["output"])
        for call in bridge._send.call_args_list
        if call.args[0]["type"] == "conversation.item.create"
    ]
    assert outputs[0] == {"ok": True, "kind": "list"}
    assert outputs[1] == {"ok": True, "kind": "now_playing"}
    assert outputs[2] == {"ok": True, "dismiss": True}
    assert outputs[3]["ok"] is False
    assert "Queue" not in json.dumps(outputs[3])
    await bridge.stop()


def test_panel_frames_flag_leaves_core_tools(monkeypatch):
    monkeypatch.setenv("STACKCHAN_PANEL_FRAMES", "0")
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=object(),
    )
    declarations = bridge._configure_tools({"tools": [{"name": "show_panel"}]})
    names = {item["name"] for item in declarations}
    assert "show_panel" not in names
    assert "dismiss_panel" not in names
    assert "show_panel" not in bridge._local_tools
    assert "show_panel" not in bridge._remote_tools
    assert "core_morph" in names
    instructions = bridge._provider_tool_instructions()
    assert "beside the orb" not in instructions
    assert "fluid SDF body" in instructions


def test_core_frames_off_still_declares_panels(monkeypatch):
    monkeypatch.setenv("STACKCHAN_CORE_FRAMES", "0")
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=object(),
    )
    names = {item["name"] for item in bridge._configure_tools({"tools": []})}
    assert "show_panel" in names
    assert "dismiss_panel" in names
    assert "core_morph" not in names


def test_audio_only_bridge_does_not_declare_panel_tools():
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        local_tools=False,
    )
    assert bridge._configure_tools({"tools": [{"name": "show_panel"}]}) == []
