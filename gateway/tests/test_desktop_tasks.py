# SPDX-License-Identifier: MIT
import asyncio
import logging
import pytest

from stackchan_mcp.desktop_tasks import (
    DESKTOP_TASK_QUIET_S,
    DESKTOP_TASK_TIMEOUT_S,
    TIMEOUT_DOWN_PHRASE,
    TIMEOUT_UP_PHRASE,
    DesktopTasks,
    failure_phrase,
    is_holding_line,
    late_phrase,
)


def _tracker(announce, *, ready=True, timeout_s=90.0):
    return DesktopTasks(announce=announce, channel_ready=lambda: ready, timeout_s=timeout_s)


def test_timeout_constant_is_ninety_seconds():
    assert DESKTOP_TASK_TIMEOUT_S == 90
    # 12s covers a second bubble of the same answer without waiting out the 90s clock.
    assert DESKTOP_TASK_QUIET_S == 12


def test_holding_lines_stay_short_progress_beats():
    assert is_holding_line("Looking up Durham events for November 14 and 15 now.")
    assert is_holding_line("Still searching…")
    assert not is_holding_line("Durham has a packed weekend, with the free Iron Pour festival.")
    assert late_phrase("DPAC lookup", "The show is at 7.") == (
        "That DPAC lookup just came back: The show is at 7."
    )


def test_failure_phrase_uses_the_request():
    assert failure_phrase("events nearby") == "I couldn't finish looking up events nearby."


@pytest.mark.asyncio
async def test_reconnect_wait_comes_out_of_the_timeout_budget():
    import time

    tasks = _tracker(None, timeout_s=90)
    tasks.start("events nearby", waited_ms=2000)
    task = next(iter(tasks._open.values()))
    remaining = task.deadline_mono - time.monotonic()
    assert 87 <= remaining <= 88.5
    assert task.waited_ms == 2000
    await tasks.aclose()


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
async def test_holding_line_then_answer_both_stay_open(caplog):
    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    tasks = _tracker(None, timeout_s=30)
    tasks.quiet_s = 30
    tasks.start("Durham events")
    now = int(__import__("time").time() * 1000)
    holding = tasks.accept_reply("e1", "Looking up Durham events for November 14 and 15 now.", now)
    answer = tasks.accept_reply("e2", "Durham has a packed weekend.", now + 1000)
    again = tasks.accept_reply("e2", "Durham has a packed weekend.", now + 1000)
    assert holding["late"] is False and answer["late"] is False
    assert holding["say"].startswith("Looking up")
    assert answer["say"] == "Durham has a packed weekend."
    assert again is None
    assert len(tasks._open) == 1
    assert not any("outcome=delivered" in r.message for r in caplog.records)
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
    assert spoken == [TIMEOUT_UP_PHRASE]
    line = next(r.getMessage() for r in caplog.records if "outcome=" in r.message)
    assert "outcome=timeout" in line
    assert "events nearby" not in line


@pytest.mark.asyncio
async def test_timeout_while_channel_down_logs_channel_down(caplog):
    spoken = []

    async def announce(text):
        spoken.append(text)
        return True

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    tasks = _tracker(announce, ready=False, timeout_s=0.02)
    tasks.start("events nearby")
    await asyncio.sleep(0.08)
    line = next(r.getMessage() for r in caplog.records if "outcome=" in r.message)
    assert "outcome=channel_down" in line
    assert spoken == [TIMEOUT_DOWN_PHRASE]


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
    assert exported[0]["text"] == TIMEOUT_UP_PHRASE
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
    now = int(__import__("time").time() * 1000)
    decision = second.accept_reply("e1", "Saturday market", now)
    assert decision["say"] == "Saturday market"
    assert len(second._open) == 1
    assert spoken == []
    assert not any("outcome=" in r.message for r in caplog.records)
    await second.aclose()


@pytest.mark.asyncio
async def test_timeout_skips_the_phrase_after_any_reply_and_a_late_reply_is_once(caplog):
    spoken = []

    async def announce(text):
        spoken.append(text)
        return True

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    tasks = _tracker(announce, ready=True, timeout_s=0.05)
    tasks.start("DPAC lookup")
    now = int(__import__("time").time() * 1000)
    tasks.accept_reply("hold", "Looking up DPAC now.", now)
    await asyncio.sleep(0.12)
    assert spoken == []
    assert any("hop=timeout_skipped" in r.message for r in caplog.records)
    late = tasks.accept_reply("real", "The show is at 7.", now + 5000)
    assert late["say"] == "That DPAC lookup just came back: The show is at 7."
    assert tasks.accept_reply("real", "The show is at 7.", now + 5000) is None
    assert not any("outcome=delivered" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_quiet_after_a_real_answer_finishes_without_a_failure_phrase():
    spoken = []

    async def announce(text):
        spoken.append(text)
        return True

    tasks = _tracker(announce, ready=True, timeout_s=30)
    tasks.quiet_s = 0.05
    tasks.start("Saturday market")
    now = int(__import__("time").time() * 1000)
    tasks.accept_reply("e1", "The market is on Saturday morning.", now)
    assert len(tasks._open) == 1
    await asyncio.sleep(0.12)
    assert tasks._open == {}
    assert spoken == []
    assert tasks._finished and tasks._finished[0].request == "Saturday market"


@pytest.mark.asyncio
async def test_gap_replies_replayed_after_reconnect_land_on_their_own_tasks(caplog):
    # 2026-11-08 16:52: Cary sent during a slow warm, Durham 25s later. All three box
    # replies landed in a 60s stream hole and arrive together on transcript replay.
    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    spoken = []

    async def announce(text):
        spoken.append(text)
        return True

    tasks = _tracker(announce, timeout_s=0.3)
    tasks.quiet_s = 30
    cary = tasks.start("Indoor events in Cary")
    durham = tasks.start("Durham events this weekend")
    base = 1_794_000_000_000
    tasks._open[cary].started_ms = base  # 21:52:06.993
    tasks._open[durham].started_ms = base + 25_127  # 21:52:32.120

    holding = tasks.accept_reply("t43s0", "I'm looking up indoor events in Cary now.", base + 7_049)
    cary_list = tasks.accept_reply("t43s1", "I found eight indoor options in Cary.", base + 23_746)
    durham_list = tasks.accept_reply("t44s0", "Durham has a busy weekend.", base + 34_375)
    replay_again = tasks.accept_reply("t43s1", "I found eight indoor options in Cary.", base + 23_746)

    assert holding["task_id"] == cary
    assert cary_list["task_id"] == cary
    assert durham_list["task_id"] == durham
    assert replay_again is None
    await asyncio.sleep(0.5)
    assert spoken == []
    messages = [r.getMessage() for r in caplog.records]
    assert any(f"id={cary} hop=timeout_skipped" in m for m in messages)
    assert any(f"id={durham} hop=timeout_skipped" in m for m in messages)
    assert not any("outcome=timeout" in m for m in messages)
    await tasks.aclose()
