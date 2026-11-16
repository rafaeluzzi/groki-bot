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
