# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Webview templates, ttl, and panel taps."""

import logging
import time
from types import SimpleNamespace

import pytest

from stackchan_mcp.gbot_voice_proxy import GbotVoiceProxy
from stackchan_mcp.panel_frames import (
    DEFAULT_TTL_S,
    PanelError,
    build_panel_frame,
    normalize_panel_document,
    panel_action_delivery,
    panel_frame_extension,
)
from stackchan_mcp.panel_templates import (
    TEMPLATE_NAMES,
    koji_fragment_error,
    render_panel_template,
)

WEATHER = {
    "place": "Durham",
    "temp": "72°",
    "condition": "Mild and dry",
    "high": "75",
    "low": "61",
}
COMPARE = {
    "title": "Shoes",
    "columns": ["Road", "Trail"],
    "rows": [
        {"label": "Weight", "values": ["10 oz", "12 oz"]},
        {"label": "Drop", "values": ["8 mm", "6 mm"]},
    ],
}
AGENDA = {"title": "Today", "items": [{"when": "9:00", "title": "Standup", "detail": "Kitchen"}]}
STEPS = {"title": "Tea", "steps": ["Boil water", {"text": "Steep", "detail": "four minutes"}]}
FACT = {"title": "Durham", "text": "A city in North Carolina.", "source": "A short note"}
SCORE = {
    "title": "Final",
    "left": {"name": "Home", "score": 2},
    "right": {"name": "Away", "score": 1},
    "detail": "Full time",
}
POINTS = {"title": "Summary", "points": ["Bring a coat", "Leave by 4"]}
CHART = {"title": "Week", "type": "bar", "labels": ["Mon", "Tue", "Wed"], "values": [1, 3, 2]}
SAMPLES = {
    "weather": WEATHER,
    "compare": COMPARE,
    "agenda": AGENDA,
    "steps": STEPS,
    "fact": FACT,
    "score": SCORE,
    "key_points": POINTS,
    "chart": CHART,
}


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_each_template_renders_sanitizer_clean_html(name):
    html = render_panel_template(name, SAMPLES[name])
    assert koji_fragment_error(html) is None
    doc, dropped = normalize_panel_document(
        {"kind": "webview", "template": name, "data": SAMPLES[name]}
    )
    assert dropped == 0
    assert doc["kind"] == "webview"
    assert doc["html"] == html
    assert doc["template"] == name
    assert "ttl_s" not in doc
    assert koji_fragment_error(doc["html"]) is None


def test_template_buttons_are_koji_actions():
    html = render_panel_template(
        "weather",
        {**WEATHER, "actions": [{"id": "more", "label": "More detail"}]},
    )
    assert 'class="btn primary"' in html
    assert 'href="koji-action:more|More detail"' in html
    assert koji_fragment_error(html) is None


def test_long_template_text_is_cut():
    html = render_panel_template(
        "fact",
        {"title": "Durham", "text": "word " * 200},
    )
    assert "…" in html
    assert koji_fragment_error(html) is None


def test_bad_template_data_names_the_field():
    with pytest.raises(PanelError, match="weather place is required"):
        normalize_panel_document({"kind": "webview", "template": "weather", "data": {}})
    with pytest.raises(PanelError, match="template must be one of"):
        normalize_panel_document({"kind": "webview", "template": "poster", "data": {}})
    with pytest.raises(PanelError, match="not both"):
        normalize_panel_document(
            {"kind": "webview", "template": "fact", "html": "<p>Hi</p>", "data": FACT}
        )
    with pytest.raises(PanelError, match="data:image"):
        normalize_panel_document(
            {
                "kind": "webview",
                "template": "fact",
                "data": {**FACT, "image": "https://example.com/a.png"},
            }
        )


def test_raw_html_still_passes_through():
    doc, _ = normalize_panel_document({"kind": "webview", "html": "<p>Hi</p>"})
    assert doc["html"] == "<p>Hi</p>"
    assert "template" not in doc


def test_ttl_defaults_stay_off_the_wire_and_overrides_pass():
    assert DEFAULT_TTL_S == {"list": 45, "choice": 60, "webview": 30, "now_playing": 0}
    listed, _ = normalize_panel_document({"kind": "list", "title": "Places", "items": ["Durham"]})
    assert "ttl_s" not in listed
    held, _ = normalize_panel_document(
        {"kind": "list", "title": "Places", "ttl_s": 10, "items": ["Durham"]}
    )
    assert held["ttl_s"] == 10
    stayed, _ = normalize_panel_document(
        {"kind": "choice", "prompt": "Tea?", "ttl_s": 0, "options": ["Yes"]}
    )
    assert stayed["ttl_s"] == 0
    playing, _ = normalize_panel_document(
        {"kind": "now_playing", "title": "Night", "show": "Ada", "ttl_s": 5}
    )
    assert "ttl_s" not in playing
    with pytest.raises(PanelError, match="ttl_s"):
        normalize_panel_document({"kind": "list", "title": "Places", "ttl_s": -1, "items": ["Durham"]})


@pytest.mark.asyncio
async def test_show_panel_logs_the_template_name(caplog):
    class Device:
        async def send_panel(self, frame):
            assert frame["panel"]["kind"] == "webview"
            assert "<script" not in frame["panel"]["html"]
            return {"ok": True}, None

    extension = panel_frame_extension(Device())
    with caplog.at_level(logging.INFO):
        result = await extension.dispatch(
            "show_panel",
            {"panel": {"kind": "webview", "template": "weather", "data": WEATHER}},
        )
    assert result == {"ok": True, "kind": "webview", "template": "weather"}
    assert "panel shown id=" in caplog.text
    assert "kind=webview" in caplog.text
    assert "template=weather" in caplog.text
    assert "items=1" in caplog.text
    assert "images=0" in caplog.text


def test_panel_action_sentence_and_local_drop():
    text, outcome = panel_action_delivery(
        {"type": "panel_action", "id": "restaurants", "action": "Durham", "label": "Durham"},
        session_live=True,
    )
    assert outcome == "delivered"
    assert text == 'User tapped "Durham" on panel "restaurants".'
    dropped, reason = panel_action_delivery(
        {"type": "panel_action", "id": "now", "action": "local:pause", "label": "Pause"},
        session_live=True,
    )
    assert dropped is None and reason == "local"
    dismissed, why = panel_action_delivery(
        {"type": "panel_action", "id": "offer", "action": "dismiss", "label": "No"},
        session_live=True,
    )
    assert dismissed is None and why == "local"
    offline, offline_why = panel_action_delivery(
        {"type": "panel_action", "id": "restaurants", "action": "Durham", "label": "Durham"},
        session_live=False,
    )
    assert offline is None and offline_why == "no_session"


class _Proxy(GbotVoiceProxy):
    def __init__(self, live: bool):
        super().__init__()
        self._live = live
        self.sent: list[str] = []

        async def send_text(text: str):
            self.sent.append(text)

        self._bridge = SimpleNamespace(send_text=send_text)

    @property
    def connected(self) -> bool:
        return self._live


@pytest.mark.asyncio
async def test_live_session_gets_the_tap_and_a_missing_session_does_not(caplog):
    tap = {"type": "panel_action", "id": "restaurants", "action": "Durham", "label": "Durham"}
    quiet = _Proxy(False)
    with caplog.at_level(logging.INFO):
        await quiet.handle_panel_action(tap)
    assert quiet.sent == []
    assert "panel_action panel=restaurants action=Durham dropped(no_session)" in caplog.text

    caplog.clear()
    live = _Proxy(True)
    with caplog.at_level(logging.INFO):
        await live.handle_panel_action(tap)
        await live.handle_panel_action(tap)
    assert live.sent == ['User tapped "Durham" on panel "restaurants".']
    assert "panel_action panel=restaurants action=Durham delivered" in caplog.text
    assert "panel outcome id=restaurants result=tapped action=Durham" in caplog.text
    assert "dropped(debounce)" in caplog.text


@pytest.mark.asyncio
async def test_panel_tap_map_drops_entries_older_than_a_few_seconds():
    live = _Proxy(True)
    now = time.monotonic()
    live._panel_taps[("old", "a")] = now - 10
    live._panel_taps[("recent", "b")] = now - 1
    live._panel_taps[("restaurants", "Durham")] = now - 0.2
    tap = {"type": "panel_action", "id": "restaurants", "action": "Durham", "label": "Durham"}
    await live.handle_panel_action(tap)
    assert ("old", "a") not in live._panel_taps
    assert ("recent", "b") in live._panel_taps
    assert live._panel_taps[("restaurants", "Durham")] >= now
    assert live.sent == []


def test_local_frame_is_not_a_panel_show():
    assert build_panel_frame("show_panel", {"panel": {"kind": "list", "items": ["A"], "ttl_s": True}}) is None
