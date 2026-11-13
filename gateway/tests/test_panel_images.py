# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Image inlining and the chart template."""

import asyncio
import base64
import io
import logging
import time

import pytest
from PIL import Image

from stackchan_mcp.panel_frames import (
    is_panel_frame,
    measure_panel,
    normalize_panel_document,
    panel_frame_extension,
)
from stackchan_mcp.panel_images import (
    MAX_DATA_URL,
    MAX_PANEL_OUT,
    cap_panel_payload,
    clear_image_cache,
    data_image_ok,
    encode_card_image,
    inline_panel_sources,
)
from stackchan_mcp.panel_templates import koji_fragment_error, render_panel_template


def _png(width: int, height: int, color: tuple[int, int, int] = (20, 80, 140)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _svg() -> bytes:
    return b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'


@pytest.fixture(autouse=True)
def _empty_cache():
    clear_image_cache()
    yield
    clear_image_cache()


def test_chart_bar_and_line_are_inlined_png():
    for kind in ("bar", "line"):
        html = render_panel_template(
            "chart",
            {"title": "Week", "type": kind, "labels": ["Mon", "Tue", "Wed"], "values": [1, 4, 2]},
        )
        assert koji_fragment_error(html) is None
        assert "data:image/png;base64," in html
        assert "<svg" not in html.lower()
        assert len(html) < 48_000
        src = html.split('src="', 1)[1].split('"', 1)[0]
        assert data_image_ok(src)
        raw = base64.b64decode(src.split(",", 1)[1])
        assert raw.startswith(b"\x89PNG")
    doc, dropped = normalize_panel_document(
        {"kind": "webview", "template": "chart", "data": {"type": "line", "labels": ["A"], "values": [2]}}
    )
    assert dropped == 0
    assert doc["template"] == "chart"
    assert is_panel_frame({"type": "panel", "panel": doc})


def test_chart_rejects_bad_data_and_not_raw_html():
    with pytest.raises(Exception, match="chart type"):
        render_panel_template("chart", {"type": "pie", "labels": ["A"], "values": [1]})
    with pytest.raises(Exception, match="chart value"):
        render_panel_template("chart", {"type": "bar", "labels": ["A"], "values": ["nope"]})


def test_encode_downscales_and_rejects_svg():
    url = encode_card_image(_png(400, 300), 160)
    assert url is not None and data_image_ok(url)
    raw = base64.b64decode(url.split(",", 1)[1])
    image = Image.open(io.BytesIO(raw))
    assert max(image.size) <= 160
    assert encode_card_image(_svg(), 160) is None
    assert len(url) <= MAX_DATA_URL


@pytest.mark.asyncio
async def test_https_image_is_inlined_and_a_broken_url_is_dropped():
    png = _png(80, 60)

    async def fetch(url: str, edge: int) -> str | None:
        if url.endswith("/missing.jpg"):
            return None
        assert url.startswith("https://")
        assert edge == 160
        return encode_card_image(png, edge)

    panel = await inline_panel_sources(
        {
            "kind": "list",
            "title": "Shows",
            "items": [
                {"title": "Alpha", "image": "https://cdn.example/a.png"},
                {"title": "Beta", "art_url": "https://cdn.example/missing.jpg"},
                {"title": "Gamma", "image": "http://cdn.example/nope.png"},
            ],
        },
        fetch=fetch,
    )
    doc, _ = normalize_panel_document(panel)
    rows = {row["label"]: row for row in doc["rows"]}
    assert data_image_ok(rows["Alpha"]["artUrl"])
    assert "artUrl" not in rows["Beta"]
    assert "artUrl" not in rows["Gamma"]
    assert is_panel_frame({"type": "panel", "panel": doc})


@pytest.mark.asyncio
async def test_a_hung_fetch_is_dropped_inside_the_budget():
    async def hang(url: str, edge: int) -> str | None:
        await asyncio.sleep(30)
        return None

    started = time.monotonic()
    panel = await inline_panel_sources(
        {"items": [{"title": "Alpha", "image": "https://cdn.example/slow.png"}]},
        fetch=hang,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 4
    assert "image" not in panel["items"][0]


@pytest.mark.asyncio
async def test_fact_image_url_is_inlined_before_render():
    async def fetch(url: str, edge: int) -> str | None:
        assert edge == 320
        return encode_card_image(_png(40, 40), edge)

    panel = await inline_panel_sources(
        {
            "kind": "webview",
            "template": "fact",
            "data": {
                "title": "Durham",
                "text": "A city in North Carolina.",
                "image": "https://cdn.example/durham.jpg",
            },
        },
        fetch=fetch,
    )
    doc, _ = normalize_panel_document(panel)
    assert doc["template"] == "fact"
    assert "data:image/jpeg;base64," in doc["html"]
    assert koji_fragment_error(doc["html"]) is None


@pytest.mark.asyncio
async def test_same_url_is_fetched_once(monkeypatch):
    downloads = {"n": 0}

    async def download(url: str, edge: int) -> str | None:
        downloads["n"] += 1
        return encode_card_image(_png(32, 32), edge)

    monkeypatch.setattr("stackchan_mcp.panel_images._download", download)
    from stackchan_mcp.panel_images import fetch_image

    first = await fetch_image("https://cdn.example/once.png", 160)
    second = await fetch_image("https://cdn.example/once.png", 160)
    assert downloads["n"] == 1
    assert first == second
    assert data_image_ok(first or "")


def test_payload_cap_drops_images_and_keeps_the_rows():
    big = "data:image/jpeg;base64," + ("A" * (MAX_DATA_URL - len("data:image/jpeg;base64,")))
    # The regex allows this. The cap cares about the JSON size.
    rows = [{"id": f"r{i}", "label": f"Row {i}", "artUrl": big} for i in range(1, 8)]
    doc = {"version": 1, "kind": "list", "id": "shows", "title": "Shows", "rows": rows}
    capped = cap_panel_payload(doc, limit=MAX_PANEL_OUT)
    assert len(capped["rows"]) == 7
    assert all(row["label"].startswith("Row") for row in capped["rows"])
    assert len(__import__("json").dumps(capped).encode()) <= MAX_PANEL_OUT
    assert any("artUrl" not in row for row in capped["rows"])


@pytest.mark.asyncio
async def test_show_panel_logs_a_list_with_an_image_and_still_sends_a_broken_one(caplog):
    png = encode_card_image(_png(24, 24), 160)

    async def fetch(url: str, edge: int) -> str | None:
        if "bad" in url:
            return None
        return png

    from stackchan_mcp import panel_images

    original = panel_images.fetch_image

    async def wrapped(url: str, edge: int) -> str | None:
        return await fetch(url, edge)

    panel_images.fetch_image = wrapped

    class Device:
        def __init__(self):
            self.frames = []

        async def send_panel(self, frame):
            assert is_panel_frame(frame)
            self.frames.append(frame)
            return {"ok": True}, None

    device = Device()
    try:
        extension = panel_frame_extension(device)
        with caplog.at_level(logging.INFO):
            listed = await extension.dispatch(
                "show_panel",
                {
                    "panel": {
                        "kind": "list",
                        "title": "Shows",
                        "items": [
                            {"title": "Alpha", "image": "https://cdn.example/a.png"},
                            {"title": "Beta", "image": "https://cdn.example/bad.png"},
                        ],
                    }
                },
            )
            closed = await extension.dispatch("dismiss_panel", {})
    finally:
        panel_images.fetch_image = original
    assert listed["ok"] is True
    rows = {row["label"]: row for row in device.frames[0]["panel"]["rows"]}
    assert data_image_ok(rows["Alpha"]["artUrl"])
    assert "artUrl" not in rows["Beta"]
    ident, kind, template, items, images, size = measure_panel(device.frames[0]["panel"])
    assert (kind, template, items, images) == ("list", "-", 2, 1)
    assert f"panel shown id={ident} kind=list template=- items=2 images=1 bytes={size}" in caplog.text
    assert closed == {"ok": True, "dismiss": True}
    assert f"panel outcome id={ident} result=dismissed-tool" in caplog.text
