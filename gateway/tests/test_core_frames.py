# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Portal type:core name mapping and the gbot voice forwarder."""

import json
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp.core_frames import (
    CORE_DECLARED_NAMES,
    CORE_INSTRUCTIONS,
    build_core_frame,
    core_action_for_tool,
    core_frame_extension,
    is_thin_core_frame,
)
from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge


@pytest.mark.parametrize(
    "name,action",
    [
        ("core_morph", "morph"),
        ("morph", "morph"),
        ("self.core.morph", "morph"),
        ("self_core_morph", "morph"),
        ("CORE.MORPH", "morph"),
        ("core_say", "say"),
        ("say", "say"),
        ("self.core.say", "say"),
        ("core_draw", "draw"),
        ("draw", "draw"),
        ("self.core.draw", "draw"),
    ],
)
def test_core_tool_names_map_to_one_action(name, action):
    assert core_action_for_tool(name) == action


@pytest.mark.parametrize("name", ["self.say", "send_task", "core", "self.core.jump", ""])
def test_unrelated_tool_names_are_not_core_actions(name):
    assert core_action_for_tool(name) is None


def test_core_instructions_name_the_body_and_the_screen():
    text = CORE_INSTRUCTIONS
    assert "fluid SDF body" in text
    assert "modes and morphs are your face" in text
    assert "what you are looking at" in text
    assert "what is on the screen" in text
    assert "Idle is a calm orb" in text
    assert "Listening, Thinking, and Speaking" in text
    assert "mode motion of the same body" in text
    assert "Morph makes the silhouette" in text
    assert "inner glow" in text
    assert "node-mesh" in text
    assert "Effects stay inside the body silhouette" in text
    assert "Do not claim the camera or a photo is on screen" in text
    assert "core_morph(name)" in text
    assert "core_say(text)" in text
    assert "core_draw(what)" in text
    assert "before the tool returns ok" in text
    assert "ask_grokbot" not in text


def test_thin_frames_match_the_portal_shape():
    assert build_core_frame("core_morph", {"name": "heart"}) == {
        "type": "core",
        "action": "morph",
        "name": "heart",
    }
    assert build_core_frame("say", {"text": " Hi "}) == {
        "type": "core",
        "action": "say",
        "text": "Hi",
    }
    assert build_core_frame("self.core.draw", {"what": "Hi"}) == {
        "type": "core",
        "action": "draw",
        "what": "Hi",
    }
    assert build_core_frame("morph", {"face": "star"})["name"] == "star"
    assert build_core_frame("core_say", {"text": "你好"})["text"] == "你好"
    assert build_core_frame("core_morph", {"name": "  "}) is None
    assert build_core_frame("core_draw", {"what": "x" * 501}) is None
    frame = build_core_frame("draw", {"what": "Hi"})
    assert is_thin_core_frame(frame)
    assert not is_thin_core_frame({**frame, "session_id": "extra"})


@pytest.mark.asyncio
async def test_extension_sends_one_frame_and_reads_the_ack():
    sent = []

    class Device:
        async def send_core(self, frame, timeout_s=3.0):
            sent.append((frame, timeout_s))
            return {"type": "core", "ok": True, "action": frame["action"]}, None

    extension = core_frame_extension(Device())
    assert [entry["name"] for entry in extension.declarations] == list(CORE_DECLARED_NAMES)
    result = await extension.dispatch("self.core.morph", {"name": "heart"})
    assert result == {"ok": True, "action": "morph"}
    assert sent == [({"type": "core", "action": "morph", "name": "heart"}, 3.0)]
    rejected = await extension.dispatch("core_say", {})
    assert rejected == {"ok": False, "error": "invalid core arguments"}
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_extension_reports_a_rejected_ack():
    class Device:
        async def send_core(self, frame, timeout_s=3.0):
            return {"type": "core", "ok": False, "action": frame["action"]}, None

    result = await core_frame_extension(Device()).dispatch("core_say", {"text": "Hi"})
    assert result["ok"] is False
    assert result["action"] == "say"
    assert "Hi" not in json.dumps(result)


@pytest.mark.asyncio
async def test_gbot_voice_forwards_core_aliases_and_leaves_unknown_native_tools(monkeypatch):
    monkeypatch.delenv("STACKCHAN_TOOL_BOT", raising=False)
    monkeypatch.delenv("STACKCHAN_TOOL_BOT_ID", raising=False)
    frames = []

    class Device:
        async def send_core(self, frame, timeout_s=3.0):
            frames.append(frame)
            return {"type": "core", "ok": True, "action": frame["action"]}, None

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
                {"name": "morph", "parameters": {"type": "object"}},
                {"name": "Self.Core.Draw", "description": "session copy"},
            ]
        }
    )
    names = {item["name"] for item in declarations}
    assert {"core_morph", "morph", "self_core_morph", "core_say", "say", "core_draw", "draw"} <= names
    assert "self_core_draw" in names
    assert "Self_Core_Draw" in names
    assert "unknown_remote_action" not in bridge._remote_tools
    assert "morph" not in bridge._remote_tools
    injected = bridge._provider_tool_instructions()
    assert CORE_INSTRUCTIONS in injected
    assert "fluid SDF body" in injected
    assert "Idle is a calm orb" in injected
    assert "node-mesh" in injected

    bridge._send = AsyncMock()
    bridge.running = True
    await bridge._tool({"name": "core_morph", "call_id": "m", "arguments": '{"name":"heart"}'})
    await bridge._tool({"name": "self_core_say", "call_id": "s", "arguments": '{"text":"Hi"}'})
    await bridge._tool({"name": "draw", "call_id": "d", "arguments": '{"what":"Hi"}'})
    await bridge._tool({"name": "Self_Core_Draw", "call_id": "alias", "arguments": '{"what":"Later"}'})
    await bridge._tool({"name": "core_morph", "call_id": "bad", "arguments": "{}"})
    assert frames == [
        {"type": "core", "action": "morph", "name": "heart"},
        {"type": "core", "action": "say", "text": "Hi"},
        {"type": "core", "action": "draw", "what": "Hi"},
        {"type": "core", "action": "draw", "what": "Later"},
    ]
    outputs = [
        json.loads(call.args[0]["item"]["output"])
        for call in bridge._send.call_args_list
        if call.args[0]["type"] == "conversation.item.create"
    ]
    assert outputs[0] == {"ok": True, "action": "morph"}
    assert outputs[-1]["ok"] is False
    await bridge.stop()


def test_core_frames_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("STACKCHAN_CORE_FRAMES", "0")
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=object(),
    )
    declarations = bridge._configure_tools(
        {"tools": [{"name": "morph", "parameters": {"type": "object"}}]}
    )
    assert "core_morph" not in {item["name"] for item in declarations}
    assert "morph" not in bridge._local_tools
    assert "morph" not in bridge._remote_tools
    instructions = bridge._provider_tool_instructions()
    assert "fluid SDF body" not in instructions
    assert "node-mesh" not in instructions
    assert "core_morph" not in instructions


def test_audio_only_bridge_does_not_declare_core_tools():
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        local_tools=False,
    )
    assert bridge._configure_tools({"tools": [{"name": "core_morph"}]}) == []
