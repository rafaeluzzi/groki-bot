# SPDX-License-Identifier: MIT
import asyncio
import logging

import pytest

from stackchan_mcp.desktop_tasks import DESKTOP_TASK_TIMEOUT_S, DesktopTasks, failure_phrase


def _tracker(announce, *, ready=True, timeout_s=90.0):
    return DesktopTasks(announce=announce, channel_ready=lambda: ready, timeout_s=timeout_s)


def test_timeout_constant_is_ninety_seconds():
    assert DESKTOP_TASK_TIMEOUT_S == 90


def test_failure_phrase_uses_the_request():
    assert failure_phrase("events nearby") == "I couldn't finish looking up events nearby."


def test_refuse_logs_channel_down_without_the_request(caplog):
    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    _tracker(None).refuse()
    line = next(r.getMessage() for r in caplog.records if r.message.startswith("desktop task"))
    assert "outcome=channel_down" in line
    assert "events" not in line
    start = line.split("start=")[1].split(" ")[0]
    end = line.split("end=")[1].split(" ")[0]
    assert start == end


@pytest.mark.asyncio
async def test_landed_result_logs_delivered_once(caplog):
    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    tasks = _tracker(None, timeout_s=30)
    tasks.start("events nearby")
    tasks.landed(spoken=True, text="There is a market on Saturday.")
    tasks.landed(spoken=True, text="extra")
    lines = [r.getMessage() for r in caplog.records if r.message.startswith("desktop task")]
    assert len(lines) == 1
    assert "outcome=delivered" in lines[0]
    assert "market" not in lines[0]
    assert "events nearby" not in lines[0]
    await tasks.aclose()


@pytest.mark.asyncio
async def test_timeout_speaks_a_failure_and_logs_timeout(caplog):
    spoken = []

    async def announce(text):
        spoken.append(text)
        return True

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    tasks = _tracker(announce, ready=True, timeout_s=0.02)
    tasks.start("events nearby")
    await asyncio.sleep(0.08)
    assert spoken == ["I couldn't finish looking up events nearby."]
    line = next(r.getMessage() for r in caplog.records if "outcome=" in r.message)
    assert "outcome=timeout" in line
    assert "events nearby" not in line


@pytest.mark.asyncio
async def test_timeout_while_channel_down_logs_channel_down(caplog):
    async def announce(text):
        return True

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    tasks = _tracker(announce, ready=False, timeout_s=0.02)
    tasks.start("events nearby")
    await asyncio.sleep(0.08)
    line = next(r.getMessage() for r in caplog.records if "outcome=" in r.message)
    assert "outcome=channel_down" in line


@pytest.mark.asyncio
async def test_unspoken_failure_is_exported_and_not_logged_yet(caplog):
    async def announce(text):
        return False

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    tasks = _tracker(announce, timeout_s=0.02)
    tasks.start("events nearby")
    await asyncio.sleep(0.08)
    assert not any("outcome=" in r.message for r in caplog.records)
    exported = tasks.export_for_handoff()
    assert exported[0]["open"] is False
    assert exported[0]["text"] == "I couldn't finish looking up events nearby."
    assert exported[0]["outcome"] == "timeout"


@pytest.mark.asyncio
async def test_exported_open_task_can_still_finish_on_the_next_tracker(caplog):
    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    first = _tracker(None, timeout_s=30)
    first.start("events nearby")
    carried = first.export_for_handoff()
    await first.aclose()
    assert carried[0]["open"] is True
    spoken = []

    async def announce(text):
        spoken.append(text)
        return True

    second = _tracker(announce, ready=True, timeout_s=30)
    second.adopt(carried[0])
    second.landed(spoken=True, text="Saturday market")
    assert spoken == []
    line = next(r.getMessage() for r in caplog.records if "outcome=" in r.message)
    assert "outcome=delivered" in line
    await second.aclose()
