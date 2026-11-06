# SPDX-License-Identifier: MIT
"""Followed-show open: panel order, misheard titles, and the podcast frame."""

import json

import pytest

from stackchan_mcp.podcast_open import followed_order, open_show, ordinal_index, plan_open


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
