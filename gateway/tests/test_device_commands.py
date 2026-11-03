# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Portal type:device_command frames and the gbot voice forwarder."""

import json
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp.device_commands import (
    DEVICE_COMMAND_DECLARED_NAMES,
    DEVICE_COMMAND_INSTRUCTIONS,
    DEVICE_COMMAND_NAMES,
    build_device_command_frame,
    device_command_extension,
    device_command_for_tool,
    device_commands_enabled,
    is_device_command_frame,
)
from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge

VOLUME = {"type": "device_command", "name": "set_volume_percent", "args": {"percent": 50}}


@pytest.mark.parametrize(
    "name",
    ["device_command", "portal_device_command", "self.device.command", "self_device_command", "Device.Command"],
)
def test_device_command_tool_names_map_to_one_tool(name):
    assert device_command_for_tool(name) == "device_command"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "open_app",
        "set_volume",
        "media_control",
        "set_volume_percent",
        "show_panel",
        "core_morph",
        "self.audio_speaker.set_volume",
    ],
)
def test_command_names_and_mac_tools_are_not_the_tool(name):
    assert device_command_for_tool(name) is None


def test_instructions_keep_portal_controls_off_the_mac():
    text = DEVICE_COMMAND_INSTRUCTIONS
    assert "stay on this device" in text
    assert "call device_command" in text
    assert "Never call set_volume, media_control, open_app" in text
    assert "desktop Spotify" in text
    assert "Mac output volume" in text
    assert "already handled the change on the device" in text
    assert '{"percent": 50}' in text
    assert "reading on_device" in text
    assert "do not invent one" in text
    assert "before the tool returns ok" in text
    for name in DEVICE_COMMAND_NAMES:
        assert name in text


def test_frames_match_the_catalog_shape():
    assert build_device_command_frame(
        "device_command",
        {"name": "set_volume_percent", "args": {"percent": 50}},
    ) == VOLUME
    assert build_device_command_frame(
        "self.device.command",
        {"name": "Set_Volume_Level", "args": json.dumps({"level": 4})},
    ) == {"type": "device_command", "name": "set_volume_level", "args": {"level": 4}}
    assert build_device_command_frame("portal_device_command", {"name": "volume_up"}) == {
        "type": "device_command",
        "name": "volume_up",
        "args": {},
    }
    assert build_device_command_frame(
        "device_command",
        {"name": "media_pause", "args": {}},
    ) == {"type": "device_command", "name": "media_pause", "args": {}}
    assert build_device_command_frame(
        "device_command",
        {"name": "set_brightness_percent", "args": {"percent": 0}},
    )["args"] == {"percent": 0}
    assert build_device_command_frame(
        "device_command",
        {"name": "open_app", "args": {"id": "spotify"}},
    ) == {"type": "device_command", "name": "open_app", "args": {"id": "spotify"}}
    assert build_device_command_frame("device_command", {"name": "get_volume", "args": {}})["name"] == "get_volume"
    assert build_device_command_frame("device_command", {"name": "show_settings"})["args"] == {}
    assert build_device_command_frame("device_command", {"name": "reconnect_bridge"})["name"] == "reconnect_bridge"
    assert is_device_command_frame(VOLUME)
    assert is_device_command_frame({"type": "device_command", "name": "volume_down", "args": {}})
    assert not is_device_command_frame({**VOLUME, "session_id": "extra"})


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"name": "set_volume"},
        {"name": "set_volume_percent"},
        {"name": "set_volume_percent", "args": {}},
        {"name": "set_volume_percent", "args": {"percent": -1}},
        {"name": "set_volume_percent", "args": {"percent": 101}},
        {"name": "set_volume_percent", "args": {"percent": 50.0}},
        {"name": "set_volume_percent", "args": {"percent": True}},
        {"name": "set_volume_percent", "args": {"percent": 50, "level": 4}},
        {"name": "set_volume_level", "args": {"percent": 50}},
        {"name": "volume_up", "args": {"percent": 50}},
        {"name": "get_volume", "args": {"percent": 50}},
        {"name": "media_skip_next", "args": {"id": "spotify"}},
        {"name": "open_app"},
        {"name": "open_app", "args": {"id": ""}},
        {"name": "open_app", "args": {"id": "spotify app"}},
        {"name": "open_app", "args": {"name": "spotify"}},
        {"name": "open_app", "args": {"id": "../spotify"}},
        {"name": "open_app", "args": {"id": "spotify/music"}},
        {"name": "brightness_up", "args": {"percent": 10}},
        {"name": "show_settings", "args": {"section": "display"}},
        {"name": "set_volume_percent", "args": {"percent": 50}, "target": "mac"},
        {"args": {"percent": 50}},
        {"name": ""},
        "set_volume_percent",
    ],
)
def test_bad_device_commands_are_rejected(arguments):
    assert build_device_command_frame("device_command", arguments) is None


def test_unrelated_tools_do_not_build_a_frame():
    assert build_device_command_frame("open_app", {"name": "open_app", "args": {"id": "spotify"}}) is None
    assert build_device_command_frame("set_volume", {"level": 50}) is None
    assert build_device_command_frame("show_panel", {"name": "volume_up"}) is None


def test_device_commands_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("STACKCHAN_DEVICE_COMMANDS", "0")
    assert device_commands_enabled() is False
    monkeypatch.setenv("STACKCHAN_DEVICE_COMMANDS", "on")
    assert device_commands_enabled() is True


@pytest.mark.asyncio
async def test_extension_sends_one_frame_and_does_not_wait_for_a_result():
    sent = []

    class Device:
        async def send_device_command(self, frame):
            sent.append(frame)
            return {"ok": True}, None

    extension = device_command_extension(Device())
    assert [entry["name"] for entry in extension.declarations] == list(DEVICE_COMMAND_DECLARED_NAMES)
    result = await extension.dispatch(
        "device_command",
        {"name": "set_volume_percent", "args": {"percent": 50}},
    )
    assert result == {"ok": True, "name": "set_volume_percent"}
    reading = await extension.dispatch("self.device.command", {"name": "get_volume"})
    assert reading == {"ok": True, "name": "get_volume", "reading": "on_device"}
    assert "percent" not in reading
    paused = await extension.dispatch("portal_device_command", {"name": "media_pause", "args": {}})
    assert paused == {"ok": True, "name": "media_pause"}
    assert sent == [
        VOLUME,
        {"type": "device_command", "name": "get_volume", "args": {}},
        {"type": "device_command", "name": "media_pause", "args": {}},
    ]
    rejected = await extension.dispatch("device_command", {"name": "set_volume_percent", "args": {"percent": 200}})
    assert rejected == {"ok": False, "error": "invalid device command arguments"}
    assert len(sent) == 3
    missed = await extension.dispatch("open_app", {"name": "spotify"})
    assert missed == {"ok": False, "error": "invalid device command arguments"}


@pytest.mark.asyncio
async def test_extension_reports_send_failure_without_the_arguments():
    class Device:
        async def send_device_command(self, frame):
            raise RuntimeError(frame["args"]["id"])

    result = await device_command_extension(Device()).dispatch(
        "device_command",
        {"name": "open_app", "args": {"id": "spotify"}},
    )
    assert result == {"ok": False, "error": "device command frame failed"}
    assert "spotify" not in json.dumps(result)

    class Offline:
        async def send_device_command(self, frame):
            return None, {"code": -32000, "message": "No ESP32 device connected"}

    offline = await device_command_extension(Offline()).dispatch("device_command", {"name": "volume_down"})
    assert offline == {"ok": False, "error": "No ESP32 device connected"}


@pytest.mark.asyncio
async def test_gbot_voice_declares_and_forwards_device_command(monkeypatch):
    monkeypatch.delenv("STACKCHAN_TOOL_BOT", raising=False)
    monkeypatch.delenv("STACKCHAN_TOOL_BOT_ID", raising=False)
    monkeypatch.delenv("STACKCHAN_DEVICE_COMMANDS", raising=False)
    monkeypatch.setenv("STACKCHAN_MAC_CONTROL", "1")
    frames = []

    class Device:
        async def send_core(self, frame, timeout_s=3.0):
            frames.append(("core", frame))
            return {"type": "core", "ok": True, "action": frame["action"]}, None

        async def send_panel(self, frame):
            frames.append(("panel", frame))
            return {"ok": True}, None

        async def send_device_command(self, frame):
            frames.append(("device", frame))
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
                {"name": "portal_device_command", "parameters": {"type": "object"}},
                {"name": "self.device.command", "parameters": {"type": "object"}},
            ]
        }
    )
    names = {item["name"] for item in declarations}
    assert "device_command" in names
    assert "portal_device_command" in names
    assert "self_device_command" in names
    assert "open_app" in names
    assert "set_volume" in names
    assert "media_control" in names
    assert "unknown_remote_action" not in bridge._remote_tools
    assert "portal_device_command" not in bridge._remote_tools
    assert "self_device_command" not in bridge._remote_tools
    assert bridge._local_tools["device_command"] == "device_command"
    assert bridge._local_tools["portal_device_command"] == "device_command"
    assert bridge._local_tools["self_device_command"] == "device_command"
    assert bridge._local_tools["open_app"] == "open_app"
    assert bridge._local_tools["set_volume"] == "set_volume"
    schema = bridge._tool_schemas["device_command"]
    assert schema["required"] == ["name"]
    assert schema["properties"]["name"]["enum"] == list(DEVICE_COMMAND_NAMES)
    injected = bridge._provider_tool_instructions()
    assert DEVICE_COMMAND_INSTRUCTIONS in injected
    assert "Never call set_volume, media_control, open_app" in injected
    assert "set_volume(level or mute) changes the volume" in injected

    bridge._send = AsyncMock()
    bridge.running = True
    await bridge._tool(
        {
            "name": "device_command",
            "call_id": "v",
            "arguments": json.dumps({"name": "set_volume_percent", "args": {"percent": 50}}),
        }
    )
    await bridge._tool(
        {
            "name": "self_device_command",
            "call_id": "a",
            "arguments": json.dumps({"name": "open_app", "args": {"id": "spotify"}}),
        }
    )
    await bridge._tool(
        {"name": "portal_device_command", "call_id": "g", "arguments": json.dumps({"name": "get_volume"})}
    )
    await bridge._tool(
        {
            "name": "device_command",
            "call_id": "bad",
            "arguments": json.dumps({"name": "set_volume_percent", "args": {"percent": 150}}),
        }
    )
    assert [item for kind, item in frames if kind == "device"] == [
        VOLUME,
        {"type": "device_command", "name": "open_app", "args": {"id": "spotify"}},
        {"type": "device_command", "name": "get_volume", "args": {}},
    ]
    outputs = [
        json.loads(call.args[0]["item"]["output"])
        for call in bridge._send.call_args_list
        if call.args[0]["type"] == "conversation.item.create"
    ]
    assert outputs[0] == {"ok": True, "name": "set_volume_percent"}
    assert outputs[1] == {"ok": True, "name": "open_app"}
    assert outputs[2] == {"ok": True, "name": "get_volume", "reading": "on_device"}
    assert outputs[3] == {"ok": False, "error": "invalid device command arguments"}
    assert "150" not in json.dumps(outputs[3])
    await bridge.stop()


def test_device_commands_flag_leaves_panel_and_core_tools(monkeypatch):
    monkeypatch.setenv("STACKCHAN_DEVICE_COMMANDS", "0")
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=object(),
    )
    declarations = bridge._configure_tools(
        {"tools": [{"name": "device_command"}, {"name": "portal_device_command"}]}
    )
    names = {item["name"] for item in declarations}
    assert "device_command" not in names
    assert "portal_device_command" not in names
    assert "device_command" not in bridge._local_tools
    assert "device_command" not in bridge._remote_tools
    assert "portal_device_command" not in bridge._remote_tools
    assert "show_panel" in names
    assert "core_morph" in names
    instructions = bridge._provider_tool_instructions()
    assert "stay on this device" not in instructions
    assert "fluid SDF body" in instructions


def test_panel_frames_off_still_declares_device_command(monkeypatch):
    monkeypatch.setenv("STACKCHAN_PANEL_FRAMES", "0")
    monkeypatch.setenv("STACKCHAN_CORE_FRAMES", "0")
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        esp32=object(),
    )
    names = {item["name"] for item in bridge._configure_tools({"tools": []})}
    assert "device_command" in names
    assert "show_panel" not in names
    assert "core_morph" not in names


def test_audio_only_bridge_does_not_declare_device_command():
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        local_tools=False,
    )
    assert bridge._configure_tools({"tools": [{"name": "device_command"}]}) == []
