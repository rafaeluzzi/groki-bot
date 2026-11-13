# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Fetch https images and draw small charts for Portal panels.

Koji's webview accepts ``data:image`` PNG, JPEG, GIF, and WebP. It rejects
SVG. List thumbnails decode with BitmapFactory, which accepts the same raster
types and not SVG. This module turns an https image into a small JPEG (or
PNG when the picture has transparency) and draws bar and line charts as PNG.

A fetch that fails, times out, or is not an image is dropped. The panel still
shows. Nothing here logs the URL or the image bytes: a signed URL can carry a
token.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import re
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

MAX_DATA_URL = 20_000
MAX_PANEL_OUT = 96_000
CARD_EDGE = 160
CONTENT_EDGE = 320
FETCH_TIMEOUT_S = 2.0
FETCH_BUDGET_S = 2.5
MAX_FETCH_BYTES = 1_500_000
CACHE_TTL_S = 600.0
FAIL_TTL_S = 30.0
CACHE_MAX = 64

_DATA_IMAGE_RE = re.compile(
    r"^data:image/(png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/]+=*$",
    re.IGNORECASE,
)
_IMAGE_KEYS = ("artUrl", "art_url", "image", "imageUrl", "image_url", "thumbnail", "thumb")
_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
_CARD_RGB = (20, 26, 34)

FetchImage = Callable[[str, int], Awaitable[str | None]]

_cache: OrderedDict[tuple[str, int], tuple[float, str | None]] = OrderedDict()
_cache_lock = asyncio.Lock()
_abandoned: set[asyncio.Task[Any]] = set()


def _abandon(task: asyncio.Task[Any]) -> None:
    _abandoned.add(task)
    task.add_done_callback(_abandoned.discard)
    task.cancel()


def data_image_ok(text: str, *, limit: int = MAX_DATA_URL) -> bool:
    """True when ``text`` is a Koji data-image URL inside the size cap."""
    return isinstance(text, str) and len(text) <= limit and _DATA_IMAGE_RE.fullmatch(text.strip()) is not None


def http_image_ok(text: str, *, limit: int = 500) -> bool:
    """An http(s) URL Koji already accepts on ``artUrl``."""
    if not isinstance(text, str) or len(text) > limit:
        return False
    if any(ch.isspace() for ch in text) or "<" in text:
        return False
    return text.startswith(("https://", "http://"))


def _remember(key: tuple[str, int], value: str | None, ttl: float) -> None:
    _cache[key] = (time.monotonic() + ttl, value)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


async def _cached(url: str, edge: int) -> str | None | object:
    """The cached data URL, None for a recent failure, or a sentinel on a miss."""
    key = (url, edge)
    async with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return _MISS
        expires, value = hit
        if expires <= time.monotonic():
            _cache.pop(key, None)
            return _MISS
        _cache.move_to_end(key)
        return value


_MISS = object()


async def fetch_image(url: str, edge: int) -> str | None:
    """Download one https image and return a small data URL, or None.

    Tests replace this function. The default speaks https only, checks the
    content type, and stops at the byte cap.
    """
    if not url.startswith("https://") or any(ch.isspace() for ch in url):
        return None
    cached = await _cached(url, edge)
    if cached is not _MISS:
        return cached if isinstance(cached, str) else None
    data_url = await _download(url, edge)
    async with _cache_lock:
        _remember((url, edge), data_url, CACHE_TTL_S if data_url else FAIL_TTL_S)
    return data_url


async def _download(url: str, edge: int) -> str | None:
    try:
        import aiohttp
    except ImportError:
        return None
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S, connect=FETCH_TIMEOUT_S, sock_read=FETCH_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                allow_redirects=True,
                headers={"Accept": "image/png,image/jpeg,image/webp,image/gif"},
            ) as response:
                final = str(response.url)
                if response.status != 200 or not final.startswith("https://"):
                    return None
                ctype = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if ctype not in _ALLOWED_TYPES:
                    return None
                length = response.content_length
                if length is not None and length > MAX_FETCH_BYTES:
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(8192):
                    total += len(chunk)
                    if total > MAX_FETCH_BYTES:
                        return None
                    chunks.append(chunk)
    except Exception:
        return None
    if not chunks:
        return None
    return encode_card_image(b"".join(chunks), edge)


def encode_card_image(raw: bytes, edge: int) -> str | None:
    """Downscale image bytes to a Koji data URL, or None when they are not a picture."""
    if not raw or edge < 1:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        return None
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    flattened = Image.new("RGB", image.size, _CARD_RGB)
    if image.mode == "RGBA":
        flattened.paste(image, mask=image.getchannel("A"))
    else:
        flattened.paste(image)
    for quality, scale in ((70, 1.0), (55, 1.0), (50, 0.75)):
        frame = flattened
        if scale < 1:
            size = (max(1, int(flattened.width * scale)), max(1, int(flattened.height * scale)))
            frame = flattened.resize(size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        frame.save(out, format="JPEG", quality=quality, optimize=True)
        data_url = "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
        if len(data_url) <= MAX_DATA_URL:
            return data_url
    return None


def chart_data_url(kind: str, labels: list[str], values: list[float]) -> str | None:
    """A small PNG data URL for a bar or line chart, or None when it will not fit."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    width, height = 520, 220
    image = Image.new("RGB", (width, height), _CARD_RGB)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()
    left, right, top, bottom = 28, width - 12, 16, height - 32
    plot_w = right - left
    plot_h = bottom - top
    accent = (50, 214, 238)
    dim = (163, 174, 187)
    grid = (44, 54, 66)
    low = min(0.0, min(values))
    high = max(0.0, max(values))
    if high - low < 1e-9:
        high = low + 1.0

    def y_of(value: float) -> float:
        return bottom - ((value - low) / (high - low)) * plot_h

    baseline = y_of(0.0)
    draw.line((left, top, left, bottom), fill=grid, width=1)
    draw.line((left, baseline, right, baseline), fill=grid, width=1)
    count = len(values)
    if kind == "bar":
        gap = 8
        bar_w = max(4, int((plot_w - gap * (count + 1)) / count))
        for index, value in enumerate(values):
            x = left + gap + index * (bar_w + gap)
            y = y_of(value)
            top_y, bot_y = (y, baseline) if value >= 0 else (baseline, y)
            draw.rectangle((x, top_y, x + bar_w, bot_y), fill=accent)
    else:
        points = []
        for index, value in enumerate(values):
            x = left + (plot_w * index / (count - 1 if count > 1 else 1))
            points.append((x, y_of(value)))
        if len(points) == 1:
            x, y = points[0]
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=accent)
        else:
            draw.line(points, fill=accent, width=3)
            for x, y in points:
                draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=accent)
    for index, label in enumerate(labels):
        if kind == "bar":
            gap = 8
            bar_w = max(4, int((plot_w - gap * (count + 1)) / count))
            x = left + gap + index * (bar_w + gap) + bar_w / 2
        else:
            x = left + (plot_w * index / (count - 1 if count > 1 else 1))
        text = label if len(label) <= 8 else label[:7] + "..."
        try:
            draw.text((x, bottom + 4), text, fill=dim, font=font, anchor="ma")
        except Exception:
            draw.text((x, bottom + 4), text, fill=dim, font=font)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    data_url = "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    if len(data_url) > MAX_DATA_URL:
        return None
    return data_url


def _collect(node: Any, found: list[tuple[dict[str, Any], str, int]], edge: int) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _IMAGE_KEYS and isinstance(value, str):
                found.append((node, key, edge))
            elif key == "data" and isinstance(value, dict):
                _collect(value, found, CONTENT_EDGE)
            elif key in ("items", "rows") and isinstance(value, list):
                for item in value:
                    _collect(item, found, edge)
    elif isinstance(node, list):
        for item in node:
            _collect(item, found, edge)


async def inline_panel_sources(panel: dict[str, Any], *, fetch: FetchImage | None = None) -> dict[str, Any]:
    """Copy ``panel`` with image sources replaced by data URLs.

    An https URL is fetched. A data URL that already fits is kept. Anything
    else, including http and a failed fetch, loses the image field.
    """
    panel = copy.deepcopy(panel)
    found: list[tuple[dict[str, Any], str, int]] = []
    _collect(panel, found, CARD_EDGE)
    if not found:
        return panel
    getter = fetch or fetch_image
    needed: dict[tuple[str, int], asyncio.Task[str | None]] = {}
    for container, key, edge in found:
        text = container[key].strip()
        container[key] = text
        if text.startswith("https://") and (text, edge) not in needed:
            needed[(text, edge)] = asyncio.create_task(getter(text, edge))
    if needed:
        _, pending = await asyncio.wait(set(needed.values()), timeout=FETCH_BUDGET_S)
        for task in pending:
            # A stuck DNS lookup does not notice cancellation until it returns.
            # Drop the picture now and let the lookup finish in the background.
            _abandon(task)
    resolved: dict[tuple[str, int], str | None] = {}
    for pair, task in needed.items():
        if task.cancelled() or not task.done():
            resolved[pair] = None
            continue
        try:
            value = task.result()
        except Exception:
            value = None
        resolved[pair] = value if isinstance(value, str) and data_image_ok(value) else None
    for container, key, edge in found:
        text = container[key]
        if data_image_ok(text):
            continue
        if text.startswith("https://"):
            inlined = resolved.get((text, edge))
            if inlined:
                container[key] = inlined
                continue
        container.pop(key, None)
    return panel


async def inline_panel_argument(value: object, *, fetch: FetchImage | None = None) -> object:
    """Inline images on a panel object. A JSON string is parsed first."""
    import json

    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
    if not isinstance(parsed, dict):
        return value
    return await inline_panel_sources(parsed, fetch=fetch)


def cap_panel_payload(document: dict[str, Any], *, limit: int = MAX_PANEL_OUT) -> dict[str, Any]:
    """Drop trailing list or cover images until the document fits.

    The card stays. Webview HTML is already capped by the template renderer.
    """
    import json

    document = copy.deepcopy(document)

    def size() -> int:
        return len(json.dumps(document, ensure_ascii=False).encode("utf-8"))

    while size() > limit:
        rows = document.get("rows")
        if isinstance(rows, list):
            for row in reversed(rows):
                if isinstance(row, dict) and row.pop("artUrl", None):
                    break
            else:
                if not document.pop("artUrl", None):
                    break
            continue
        if not document.pop("artUrl", None):
            break
    return document


def clear_image_cache() -> None:
    """Test helper."""
    _cache.clear()
