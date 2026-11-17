# SPDX-License-Identifier: MIT
"""Followed-show open: panel order, misheard titles, and the podcast frame."""

import asyncio
import json
import logging

import pytest

from stackchan_mcp.podcast_open import (
    PODCAST_INSTRUCTIONS,
    channel_title,
    fill_blank_titles,
    followed_order,
    is_podcast_frame,
    open_show,
    ordinal_index,
    plan_open,
    podcast_declarations,
    podcast_frame,
    podcast_open_extension,
)


LEX = {"id": "lex", "title": "Lex Fridman Podcast", "spotifyShowUri": "spotify:show:2MAi0BvDc6GTFvKFPXnkCL"}
DAILY = {"id": "daily", "title": "The Daily", "spotifyShowUri": "spotify:show:daily"}
PLANET = {"id": "pm", "title": "Planet Money", "spotifyShowUri": "spotify:show:pm"}
SHOWS = [PLANET, LEX, DAILY]


def test_one_voice_tells_grok_to_say_the_line():
    from types import SimpleNamespace

    quiet = SimpleNamespace(one_voice=False)
    legacy = podcast_open_extension(quiet)
    assert legacy.instructions_for() == PODCAST_INSTRUCTIONS
    assert "do not say anything before or after" in legacy.declarations_for()[0]["description"].lower()
    heard = SimpleNamespace(one_voice=True)
    voiced = podcast_open_extension(heard)
    text = voiced.instructions_for()
    assert "The Portal does not speak" in text
    assert "say one short line from say" in text
    assert "do not say anything before or after" not in voiced.declarations_for()[0]["description"].lower()


def test_the_tool_tells_the_model_not_to_speak_around_it():
    description = podcast_declarations()[0]["description"]
    assert "do not say anything before or after" in description.lower()
    assert "A-to-Z" in description
    extension = podcast_open_extension(object())
    assert "do not say anything before or after" in extension.instructions.lower()
    assert extension.instructions == PODCAST_INSTRUCTIONS
    assert "number" in extension.instructions.lower()
    assert ordinal_index("number three") == 3
    plan = plan_open("number 2", SHOWS)
    assert plan == {"kind": "open", "title": "Planet Money"}


def test_followed_order_matches_the_router_title_sort():
    ordered = [show["title"] for show in followed_order(SHOWS)]
    assert ordered == ["Lex Fridman Podcast", "Planet Money", "The Daily"]
    assert [show["title"] for show in followed_order(list(reversed(SHOWS)))] == ordered


def test_number_six_is_not_a_title_and_number_two_is_the_second_row():
    assert ordinal_index("number six") == 6
    assert ordinal_index("play number six") is None
    plan = plan_open("number six", SHOWS)
    assert plan["kind"] == "range"
    assert plan["result"]["reason"] == "out_of_range"
    assert "shows" not in plan["result"]
    assert "name these" not in json.dumps(plan["result"]).lower()
    picked = plan_open("number 2", SHOWS)
    assert picked == {"kind": "open", "title": "Planet Money"}


@pytest.mark.asyncio
async def test_legs_freeman_misses_and_lists_the_panel_order():
    async def post_open(title):
        raise AssertionError(title)

    result, frame = await open_show("legs freeman podcasts", SHOWS, post_open)
    assert frame is None
    assert result["ok"] is False
    assert result["matched"] is False
    assert result["reason"] == "not_followed"
    assert result["did_you_mean"] == "Lex Fridman Podcast"
    assert "shows" not in result
    assert "candidates" not in result
    assert "call_again" not in result
    assert "name these" not in json.dumps(result).lower()
    vague, vague_frame = await open_show("zzzzqqqq", SHOWS, post_open)
    assert vague_frame is None
    assert vague["reason"] == "not_followed"
    assert "did_you_mean" not in vague


@pytest.mark.asyncio
async def test_a_corrected_title_sends_one_podcast_frame():
    async def post_open(title):
        assert title == "Lex Fridman Podcast"
        return {
            "ok": True,
            "showId": "lex",
            "showTitle": title,
            "announce": f"Opening {title} in Spotify.",
            "openSpotifyUri": LEX["spotifyShowUri"],
            "openSpotifyUrl": "https://open.spotify.com/show/2MAi0BvDc6GTFvKFPXnkCL",
        }

    result, frame = await open_show("Lex Fridman", SHOWS, post_open)
    assert result["spoken"] is True
    assert "do not say anything before or after" in result["note"].lower()
    assert "do not repeat" in result["note"].lower()
    assert frame["type"] == "podcast"
    assert frame["openSpotifyUri"] == LEX["spotifyShowUri"]
    assert frame["announce"].startswith("Opening Lex Fridman Podcast")
    assert json.loads(json.dumps(frame))["type"] == "podcast"


@pytest.mark.asyncio
async def test_number_one_opens_the_first_panel_row():
    seen = {}

    async def post_open(title):
        seen["title"] = title
        return {
            "showId": "lex",
            "announce": "Opening Lex Fridman Podcast in Spotify.",
            "openSpotifyUri": LEX["spotifyShowUri"],
        }

    result, frame = await open_show("number one", SHOWS, post_open)
    assert seen["title"] == "Lex Fridman Podcast"
    assert frame["type"] == "podcast"
    assert result["openSpotifyUri"] == LEX["spotifyShowUri"]


def test_a_blank_title_is_last_and_has_no_number():
    shows = [{"title": ""}, {"title": "Planet Money"}, {"title": "All-In"}]
    assert [show["title"] for show in followed_order(shows)] == ["All-In", "Planet Money", ""]
    rows = plan_open("number 2", shows)
    assert rows == {"kind": "open", "title": "Planet Money"}
    missed = plan_open("number 3", shows)
    assert missed["kind"] == "range"
    assert missed["result"]["reason"] == "out_of_range"
    assert "shows" not in missed["result"]


def test_channel_title_reads_the_first_channel_title():
    payload = b"<rss><channel><atom:link title=\"MP3 Audio\"/><title>The Daily</title><item><title>Ep</title></item></channel></rss>"
    assert channel_title(payload) == "The Daily"
    assert channel_title(b"<rss></rss>") == ""


@pytest.mark.asyncio
async def test_a_blank_title_uses_the_feed_name_before_numbering():
    shows = [
        {"title": "", "feedUrl": "https://feeds.example/daily"},
        {"title": "Planet Money"},
        {"title": "Lex Fridman Podcast"},
    ]

    async def read(url):
        assert url.endswith("/daily")
        return "The Daily"

    filled = await fill_blank_titles(shows, read)
    assert [show["title"] for show in followed_order(filled)] == [
        "Lex Fridman Podcast",
        "Planet Money",
        "The Daily",
    ]
    assert plan_open("number 2", filled)["title"] == "Planet Money"
    assert plan_open("number 3", filled)["title"] == "The Daily"


@pytest.mark.asyncio
async def test_an_unreadable_blank_title_stays_unnumbered():
    async def read(_url):
        return ""

    filled = await fill_blank_titles([{"title": "", "feedUrl": "https://feeds.example/x"}, {"title": "Hard Fork"}], read)
    assert plan_open("number 1", filled)["title"] == "Hard Fork"
    assert plan_open("number 2", filled)["kind"] == "range"


def test_a_show_open_keeps_the_enclosure_on_the_frame():
    body = {
        "showId": "hf",
        "showTitle": "Hard Fork",
        "announce": "I'll open Hard Fork in Spotify. The latest episode is Jensen Huang Is Out at Nvidia.",
        "openSpotifyUri": "spotify:show:44fllCS2FTFr2x2kjP9xeT",
        "openKind": "show",
        "rssEpisode": {
            "guid": "ep",
            "title": "Jensen Huang Is Out at Nvidia",
            "enclosureUrl": "https://cdn.example/hf.mp3",
            "showTitle": "Hard Fork",
        },
    }
    frame = podcast_frame(body)
    assert frame["openKind"] == "show"
    assert frame["rssEpisode"]["enclosureUrl"] == "https://cdn.example/hf.mp3"
    assert is_podcast_frame(frame)
    assert "query" not in frame["rssEpisode"]["enclosureUrl"]


def test_ordinal_uses_the_visible_row_or_the_az_list():
    panel = {
        "id": "suggestions",
        "title": "Podcast-suggestions",
        "rows": [
            {"n": 1, "label": "The Journal"},
            {"n": 2, "label": "Hard Fork"},
            {"n": 3, "label": "Decoder"},
        ],
    }
    followed = [{"title": "Hard Fork"}, {"title": "Planet Money"}]
    assert plan_open("", followed, ordinal=2, panel=panel) == {"kind": "open", "title": "Hard Fork"}
    missed = plan_open("", followed, ordinal=3, panel=panel)
    assert missed["result"]["reason"] == "not_followed"
    assert missed["result"]["query"] == "Decoder"
    assert "shows" not in missed["result"]
    assert plan_open("", followed, ordinal=9, panel=panel)["result"]["reason"] == "out_of_range"
    assert plan_open("", followed, ordinal=1)["title"] == "Hard Fork"
    assert plan_open("", followed, ordinal=2)["title"] == "Planet Money"
    assert plan_open("", followed, ordinal=4)["result"]["reason"] == "out_of_range"
    playing = {"id": "now", "kind": "now_playing", "title": "Episode", "rows": [{"n": 1, "label": "Decoder"}]}
    assert plan_open("", followed, ordinal=2, panel=playing)["title"] == "Planet Money"


class _Host:
    def __init__(self):
        self.frames = []
        self.listen_starts = 1
        self.koji_context = None

    async def send_podcast(self, frame):
        self.frames.append(frame)
        return {"ok": True}, None


def _patch_catalog(monkeypatch, shows):
    async def followed():
        return shows

    async def post(title):
        return {
            "showId": "x",
            "showTitle": title,
            "announce": f"Opening {title} in Spotify.",
            "openSpotifyUri": "spotify:show:x",
        }

    monkeypatch.setattr("stackchan_mcp.podcast_open._get_followed", followed)
    monkeypatch.setattr("stackchan_mcp.podcast_open._post_open", post)


@pytest.mark.asyncio
async def test_one_open_per_turn_and_a_fourth_miss_is_capped(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="stackchan_mcp.podcast_turn")
    shows = [{"id": str(i), "title": f"Show {i}"} for i in range(6)]
    _patch_catalog(monkeypatch, shows)
    host = _Host()
    host.koji_context = {"seq": 7, "panel": None}
    extension = podcast_open_extension(host)
    results = await asyncio.gather(*(extension.dispatch("podcast_open_spotify", {"show": f"Show {i}"}) for i in range(6)))
    assert len(host.frames) == 1
    opened = [row for row in results if row.get("ok") is True]
    blocked = [row for row in results if row.get("reason") == "one_open_per_turn"]
    assert len(opened) == 1
    assert len(blocked) == 5
    assert blocked[0]["opened"] == opened[0]["title"]
    assert "podcast guard seq=7 reason=one_open_per_turn" in caplog.text
    for title in (f"Show {i}" for i in range(6)):
        assert title not in caplog.text

    host.koji_context = {"seq": 8, "panel": None}
    again = await extension.dispatch("podcast_open_spotify", {"show": "Show 1"})
    assert again["ok"] is True
    assert len(host.frames) == 2

    misses = []
    host.koji_context = {"seq": 9, "panel": None}

    async def no_post(title):
        misses.append(title)
        return {}

    monkeypatch.setattr("stackchan_mcp.podcast_open._post_open", no_post)
    looked = []

    async def count_followed():
        looked.append(1)
        return shows

    monkeypatch.setattr("stackchan_mcp.podcast_open._get_followed", count_followed)
    fresh = podcast_open_extension(host)
    outcomes = []
    for _ in range(4):
        outcomes.append(await fresh.dispatch("podcast_open_spotify", {"show": "not a show"}))
    assert [row.get("reason") for row in outcomes[:3]] == ["not_followed", "not_followed", "not_followed"]
    assert outcomes[3]["reason"] == "capped"
    assert misses == []
    assert len(looked) == 3
    assert "podcast guard seq=9 reason=capped" in caplog.text


@pytest.mark.asyncio
async def test_listen_starts_reset_the_guard_when_there_is_no_context(monkeypatch):
    _patch_catalog(monkeypatch, [{"id": "hf", "title": "Hard Fork"}])
    host = _Host()
    extension = podcast_open_extension(host)
    first = await extension.dispatch("podcast_open_spotify", {"show": "Hard Fork"})
    assert first["ok"] is True
    second = await extension.dispatch("podcast_open_spotify", {"show": "Hard Fork"})
    assert second["reason"] == "one_open_per_turn"
    host.listen_starts = 2
    third = await extension.dispatch("podcast_open_spotify", {"show": "Hard Fork"})
    assert third["ok"] is True


@pytest.mark.asyncio
async def test_open_keeps_the_seq_from_when_the_call_started(monkeypatch):
    _patch_catalog(monkeypatch, [{"id": "hf", "title": "Hard Fork"}])

    class Host(_Host):
        def __init__(self):
            super().__init__()
            self.koji_context = {"seq": 4, "panel": None}
            self.seen = None

        async def send_podcast(self, frame, turn_seq=None):
            self.seen = turn_seq
            self.koji_context = {"seq": 9, "panel": None}
            self.frames.append(frame)
            return {"ok": True, "status": "opening", "show": "Hard Fork", "target": "spotify", "say": "Opening."}, None

    host = Host()
    await podcast_open_extension(host).dispatch("podcast_open_spotify", {"show": "Hard Fork"})
    assert host.seen == 4


@pytest.mark.asyncio
async def test_a_new_connection_can_open_the_same_seq(monkeypatch):
    _patch_catalog(monkeypatch, [{"id": "hf", "title": "Hard Fork"}])
    host = _Host()
    host.koji_context = {"seq": 1, "panel": None}
    host.connection_generation = 1
    extension = podcast_open_extension(host)
    assert (await extension.dispatch("podcast_open_spotify", {"show": "Hard Fork"}))["ok"] is True
    blocked = await extension.dispatch("podcast_open_spotify", {"show": "Hard Fork"})
    assert blocked["reason"] == "one_open_per_turn"
    host.connection_generation = 2
    assert (await extension.dispatch("podcast_open_spotify", {"show": "Hard Fork"}))["ok"] is True
    assert len(host.frames) == 2


@pytest.mark.asyncio
async def test_the_guard_uses_the_seq_captured_at_dispatch(monkeypatch):
    _patch_catalog(monkeypatch, [{"id": "hf", "title": "Hard Fork"}])
    host = _Host()
    host.koji_context = {"seq": 4, "panel": None}
    from stackchan_mcp.podcast_turn import shared_guard

    guard = shared_guard(host)
    real = guard.lock

    class SwapLock:
        async def __aenter__(self):
            await real.acquire()
            host.koji_context = {"seq": 9, "panel": None}
            return self

        async def __aexit__(self, exc_type, exc, tb):
            real.release()
            return False

    guard.lock = SwapLock()
    assert (await podcast_open_extension(host).dispatch("podcast_open_spotify", {"show": "Hard Fork"}))["ok"] is True
    assert guard._key == ("seq", 4, 0)


@pytest.mark.asyncio
async def test_a_stale_answer_does_not_use_the_open(monkeypatch):
    _patch_catalog(monkeypatch, [{"id": "hf", "title": "Hard Fork"}])

    class Host(_Host):
        def __init__(self):
            super().__init__()
            self.koji_context = {"seq": 4, "panel": None}

        async def send_podcast(self, frame, turn_seq=None):
            self.frames.append(frame)
            status = "stale" if len(self.frames) == 1 else "opening"
            return {"ok": True, "status": status, "show": "Hard Fork", "target": "spotify", "say": "Opening."}, None

    host = Host()
    extension = podcast_open_extension(host)
    first = await extension.dispatch("podcast_open_spotify", {"show": "Hard Fork"})
    assert first["status"] == "stale"
    second = await extension.dispatch("podcast_open_spotify", {"show": "Hard Fork"})
    assert second["ok"] is True
    assert second["status"] == "opening"
    assert len(host.frames) == 2
