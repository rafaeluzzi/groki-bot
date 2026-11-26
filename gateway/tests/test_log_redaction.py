# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Tool and transcript logs keep keys, sizes, and fixed command names."""

import logging

import pytest

from stackchan_mcp.debug_status import DebugStatus
from stackchan_mcp.gemini_voice_proxy import GeminiVoiceProxy
from stackchan_mcp.mac_control import MacController
from stackchan_mcp.panel_frames import panel_frame_extension
from stackchan_mcp.voice_tools import VoiceToolExecutor

TITLE = "Nightly Astronomy Hour"
ROW = "Followed Show Alpha"
PHRASE = "who founded Nvidia?"
QUERY = "who founded Nvidia weather special"


class _PanelDevice:
    async def send_panel(self, frame):
        return {"ok": True}, None


class _Runner:
    async def __call__(self, *argv, **kwargs):
        return 0, "", ""


@pytest.mark.asyncio
async def test_show_panel_and_transcript_logs_omit_words(caplog):
    status = DebugStatus()
    panel = {
        "version": 1,
        "kind": "list",
        "id": "followed",
        "title": TITLE,
        "rows": [{"id": "a", "label": ROW}],
    }
    executor = VoiceToolExecutor(
        extensions=(panel_frame_extension(_PanelDevice()),),
        debug_status=status,
    )
    proxy = GeminiVoiceProxy(debug_status=status)
    with caplog.at_level(logging.INFO):
        result = await executor._dispatch_tool("show_panel", {"panel": panel})
        executor._log_tool_result(
            "device_command",
            {"name": "set_volume_percent", "args": {"percent": 30}},
            {"ok": True},
        )
        executor._log_tool_result(
            "device_command",
            {"name": "please play this song", "args": {}},
            {"ok": False, "error": "rejected"},
        )
        await proxy._log_gemini_transcript(PHRASE)
        await MacController(run_cmd=_Runner()).dispatch(
            "web_search", {"query": QUERY, "engine": "bing"}
        )

    assert result["ok"] is True
    text = caplog.text
    assert TITLE not in text
    assert ROW not in text
    assert PHRASE not in text
    assert QUERY not in text
    assert "please play this song" not in text
    assert "args={" not in text
    assert "gemini transcript: chars=" in text
    assert "name=show_panel" in text
    assert "args_keys=panel" in text
    assert "n=1" in text
    assert "bytes=" in text
    assert "command=set_volume_percent" in text
    command_line = next(line for line in text.splitlines() if "command=set_volume_percent" in line)
    assert "30" not in command_line
    assert "percent" not in command_line.replace("set_volume_percent", "")
    assert "web_search engine=bing chars=" in text
    stored = status.snapshot()["recent"]["transcripts"]
    assert stored[0]["chars"] == len(PHRASE)
    assert "text" not in stored[0]
