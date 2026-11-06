# SPDX-License-Identifier: MIT
"""Followed-show open: panel order, misheard titles, and the podcast frame."""

import json

import pytest

from stackchan_mcp.podcast_open import (
    channel_title,
    fill_blank_titles,
    followed_order,
    open_show,
    ordinal_index,
    plan_open,
)


LEX = {"id": "lex", "title": "Lex Fridman Podcast", "spotifyShowUri": "spotify:show:2MAi0BvDc6GTFvKFPXnkCL"}
DAILY = {"id": "daily", "title": "The Daily", "spotifyShowUri": "spotify:show:daily"}
PLANET = {"id": "pm", "title": "Planet Money", "spotifyShowUri": "spotify:show:pm"}
SHOWS = [PLANET, LEX, DAILY]


def test_followed_order_matches_the_router_title_sort():
    ordered = [show["title"] for show in followed_order(SHOWS)]
    assert ordered == ["Lex Fridman Podcast", "Planet Money", "The Daily"]
    assert [show["title"] for show in followed_order(list(reversed(SHOWS)))] == ordered


def test_number_six_is_not_a_title_and_number_two_is_the_second_row():
    assert ordinal_index("number six") == 6
    assert ordinal_index("play number six") is None
    plan = plan_open("number six", SHOWS)
    assert plan["kind"] == "range"
    assert "no show number 6" in plan["result"]["error"]
    picked = plan_open("number 2", SHOWS)
    assert picked == {"kind": "open", "title": "Planet Money"}
    assert [row["title"] for row in plan["result"]["shows"]] == [
        "Lex Fridman Podcast",
        "Planet Money",
        "The Daily",
    ]


@pytest.mark.asyncio
async def test_legs_freeman_misses_and_lists_the_panel_order():
    async def post_open(title):
        raise AssertionError(title)

    result, frame = await open_show("legs freeman podcasts", SHOWS, post_open)
    assert frame is None
    assert result["ok"] is False
    assert result["matched"] is False
    assert result["call_again"] == "Lex Fridman Podcast"
    assert "once more" in result["instruction"]
    assert "not for you" in result["instruction"]
    assert [row["n"] for row in result["shows"]] == [1, 2, 3]
    assert result["shows"][0]["title"] == "Lex Fridman Podcast"


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
    assert result["note"].startswith("The Portal already said")
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
    assert missed["result"]["shows"] == [
        {"n": 1, "title": "All-In"},
        {"n": 2, "title": "Planet Money"},
        {"title": ""},
    ]


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
