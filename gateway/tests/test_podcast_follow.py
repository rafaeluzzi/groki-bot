# SPDX-License-Identifier: MIT
"""Follow a batch once per turn, including the labels on screen."""

import pytest

from stackchan_mcp.podcast_follow import podcast_follow_extension


class _Host:
    def __init__(self, panel=None, seq=1):
        self.koji_context = {"seq": seq, "panel": panel}
        self.listen_starts = 0


@pytest.mark.asyncio
async def test_follow_batch_from_screen_and_a_second_call(monkeypatch):
    posted = []

    async def post(title):
        posted.append(title)
        if title == "Hard Fork":
            return {"ok": True, "status": "already", "title": title}
        return {"ok": True, "status": "followed", "title": title, "show": {"title": title}}

    monkeypatch.setattr("stackchan_mcp.podcast_follow._post_follow", post)
    panel = {
        "id": "suggestions",
        "title": "Podcast-suggestions",
        "rows": [
            {"n": 1, "label": "Decoder"},
            {"n": 2, "label": "Pivot"},
        ],
    }
    host = _Host(panel)
    extension = podcast_follow_extension(host)
    screen = await extension.dispatch(
        "podcast_follow",
        {"from_screen": True, "shows": ["Ignored"]},
    )
    assert screen["ok"] is True
    assert posted == ["Decoder", "Pivot"]
    assert "Decoder" in screen["say"]
    assert "Ignored" not in screen["say"]

    host.koji_context = {"seq": 2, "panel": None}
    batch = await extension.dispatch("podcast_follow", {"shows": ["Decoder", "Hard Fork"]})
    assert [row["status"] for row in batch["results"]] == ["followed", "already"]
    assert posted[-2:] == ["Decoder", "Hard Fork"]

    again = await extension.dispatch("podcast_follow", {"shows": ["Pivot"]})
    assert again == {"ok": False, "reason": "already_handled"}
    assert posted[-1] == "Hard Fork"

    host.koji_context = {"seq": 3, "panel": None}
    reset = await extension.dispatch("podcast_follow", {"shows": ["Decoder"]})
    assert reset["ok"] is True
    assert posted[-1] == "Decoder"


@pytest.mark.asyncio
async def test_follow_titles_run_together_and_the_batch_stays_bounded(monkeypatch):
    import asyncio
    import time

    from stackchan_mcp.podcast_follow import FOLLOW_BATCH_S

    assert FOLLOW_BATCH_S <= 20

    async def post(title):
        await asyncio.sleep(0.2)
        return {"status": "followed", "title": title}

    monkeypatch.setattr("stackchan_mcp.podcast_follow._post_follow", post)
    host = _Host()
    started = time.monotonic()
    result = await podcast_follow_extension(host).dispatch(
        "podcast_follow",
        {"shows": ["One", "Two", "Three", "Four"]},
    )
    assert time.monotonic() - started < 0.6
    assert [row["status"] for row in result["results"]] == ["followed", "followed", "followed", "followed"]

    async def slow(title):
        await asyncio.sleep(1)
        return {"status": "followed", "title": title}

    monkeypatch.setattr("stackchan_mcp.podcast_follow._post_follow", slow)
    monkeypatch.setattr("stackchan_mcp.podcast_follow.FOLLOW_BATCH_S", 0.05)
    host.koji_context = {"seq": 8, "panel": None}
    started = time.monotonic()
    late = await podcast_follow_extension(host).dispatch("podcast_follow", {"shows": ["Late"]})
    assert time.monotonic() - started < 0.4
    assert late["results"] == [{"title": "Late", "status": "pending"}]
    assert "Still adding Late" in late["say"]


@pytest.mark.asyncio
async def test_a_batch_timeout_keeps_the_finished_results(monkeypatch):
    import asyncio

    async def post(title):
        if title == "Slow":
            await asyncio.sleep(1)
        return {"status": "followed", "title": title}

    monkeypatch.setattr("stackchan_mcp.podcast_follow._post_follow", post)
    monkeypatch.setattr("stackchan_mcp.podcast_follow.FOLLOW_BATCH_S", 0.05)
    host = _Host()
    result = await podcast_follow_extension(host).dispatch("podcast_follow", {"shows": ["Fast", "Slow"]})
    assert result["results"] == [
        {"title": "Fast", "status": "followed"},
        {"title": "Slow", "status": "pending"},
    ]
    assert "Followed Fast" in result["say"]
    assert "Still adding Slow" in result["say"]


@pytest.mark.asyncio
async def test_a_client_timeout_is_pending_and_fits_the_batch(monkeypatch):
    import asyncio

    from stackchan_mcp.podcast_follow import FOLLOW_BATCH_S, FOLLOW_CLIENT_S

    assert FOLLOW_BATCH_S <= 20
    assert 18 < FOLLOW_CLIENT_S < FOLLOW_BATCH_S

    async def timed_out(title):
        raise asyncio.TimeoutError

    monkeypatch.setattr("stackchan_mcp.podcast_follow._post_follow", timed_out)
    host = _Host()
    result = await podcast_follow_extension(host).dispatch("podcast_follow", {"shows": ["Slow"]})
    assert result["results"] == [{"title": "Slow", "status": "pending"}]
    assert "Still adding Slow" in result["say"]
