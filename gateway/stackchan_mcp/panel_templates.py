# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Webview templates that render to a Koji-safe HTML fragment.

Koji's webview allows a fixed tag set, class names, and ``koji-action:``
links. A template fills those from ``data`` so the model does not write HTML.
Text over a cap is cut with an ellipsis. A missing field raises ``PanelError``
with the field to fix. The fragment is what ``show_panel`` puts in ``html``.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .panel_frames import PanelError

TEMPLATE_NAMES = ("weather", "compare", "agenda", "steps", "fact", "score", "key_points", "chart")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/]+=*$",
    re.IGNORECASE,
)

# Mirrors PanelHtml.kt. Gateway tests reject a fragment this would.
_ALLOWED_TAGS = {
    "a", "p", "h1", "h2", "h3", "div", "span", "br", "hr", "ul", "ol", "li",
    "strong", "em", "b", "i", "small", "table", "thead", "tbody", "tr", "th",
    "td", "img", "section", "header", "footer", "article", "figure",
    "figcaption", "blockquote", "dl", "dt", "dd", "sup", "sub", "code", "pre",
}
_SAFE_ATTRS = {
    "class", "id", "title", "alt", "colspan", "rowspan", "headers", "scope",
    "role", "aria-label", "aria-hidden", "width", "height", "span", "start",
    "reversed",
}
_NAMED_ENTITIES = {"amp", "lt", "gt", "quot", "apos", "nbsp"}
_ATTR_RE = re.compile(
    r"""([A-Za-z_:][-A-Za-z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?"""
)
_ENTITY_RE = re.compile(r"&(#x?[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);")


def render_panel_template(name: object, data: object) -> str:
    """Return a sanitizer-clean fragment, or raise ``PanelError``."""
    if not isinstance(name, str) or name not in TEMPLATE_NAMES:
        allowed = ", ".join(TEMPLATE_NAMES)
        raise PanelError(f"template must be one of: {allowed}")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PanelError("template data must be an object")
    return _RENDERERS[name](data)


def koji_fragment_error(html: str) -> str | None:
    """A short reason Koji would reject ``html``, or None when it would render."""
    if not isinstance(html, str) or not html.strip():
        return "empty"
    if len(html) > 48_000:
        return "too long"
    if "<!--" in html or "-->" in html or "<![" in html or "<?" in html:
        return "comment or processing instruction"
    if "&#" in html:
        return "numeric entity"
    for match in _ENTITY_RE.finditer(html):
        body = match.group(1)
        if body.startswith("#") or body.lower() not in _NAMED_ENTITIES:
            return "entity"
    lower = html.lower()
    if "<script" in lower or "javascript:" in lower or " style=" in lower or " style='" in lower:
        return "script or style"
    try:
        _walk_fragment(html)
    except PanelError as exc:
        return str(exc)
    return None


def _clip(value: object, limit: int) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = str(int(value)) if value == int(value) else str(value)
    if not isinstance(value, str):
        return None
    text = "".join(" " if ord(ch) < 32 else ch for ch in value).strip()
    text = text.replace("<", "\u2039")
    if not text:
        return None
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _req(data: dict[str, Any], key: str, limit: int, what: str) -> str:
    text = _clip(data.get(key), limit)
    if text is None:
        raise PanelError(f"{what} is required")
    return text


def _opt(data: dict[str, Any], key: str, limit: int) -> str | None:
    if key not in data or data.get(key) is None:
        return None
    return _clip(data.get(key), limit)


def _token(value: object, label: str, fallback: str) -> str:
    if isinstance(value, str) and _TOKEN_RE.fullmatch(value.strip()):
        return value.strip()
    raw = label.strip() or fallback
    out: list[str] = []
    dash = False
    for ch in raw:
        if ch.isascii() and (ch.isalnum() or ch in "._:"):
            if len(out) < 60:
                out.append(ch)
            dash = False
        elif not dash and out and len(out) < 60:
            out.append("-")
            dash = True
    text = "".join(out).rstrip("-")
    if not text or not text[0].isalnum():
        return fallback
    return text


def _buttons(data: dict[str, Any], where: str) -> str:
    raw = data.get("actions")
    if raw is None:
        return ""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
        raise PanelError(f"{where} actions must be 1 or 2 buttons")
    parts: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            raise PanelError(f"{where} action {index} needs a label")
        label = _clip(item.get("label") or item.get("title") or item.get("text"), 40)
        if label is None:
            raise PanelError(f"{where} action {index} needs a label")
        ident = _token(item.get("id"), label, f"action-{index}")
        if ident in seen or ident.startswith("local:") or ident == "dismiss":
            raise PanelError(f"{where} action {index} needs its own id that is not local")
        seen.add(ident)
        href_label = label.replace("|", "/")
        kind = "btn primary" if index == 1 else "btn"
        parts.append(
            f'<a class="{kind}" href="koji-action:{ident}|{_esc(href_label)}">{_esc(label)}</a>'
        )
    return "".join(parts)


def _title(data: dict[str, Any]) -> str | None:
    return _opt(data, "title", 80)


def _weather(data: dict[str, Any]) -> str:
    place = _req(data, "place", 80, "weather place")
    temp = _req(data, "temp", 24, "weather temp")
    condition = _req(data, "condition", 80, "weather condition")
    high = _opt(data, "high", 16)
    low = _opt(data, "low", 16)
    detail = _opt(data, "detail", 160)
    bits: list[str] = []
    if high and low:
        bits.append(f"High {high}, low {low}")
    elif high:
        bits.append(f"High {high}")
    elif low:
        bits.append(f"Low {low}")
    if detail:
        bits.append(detail)
    extra = f'<p class="dim">{_esc(" · ".join(bits))}</p>' if bits else ""
    return (
        f'<p class="kicker">{_esc(place)}</p>'
        f'<p class="stat">{_esc(temp)}</p>'
        f"<p>{_esc(condition)}</p>"
        f"{extra}"
        f"{_buttons(data, 'weather')}"
    )


def _compare(data: dict[str, Any]) -> str:
    columns = data.get("columns")
    if not isinstance(columns, list) or not 2 <= len(columns) <= 4:
        raise PanelError("compare needs columns: 2 to 4 names")
    names: list[str] = []
    for index, column in enumerate(columns, start=1):
        name = _clip(column, 40)
        if name is None:
            raise PanelError(f"compare column {index} needs a name")
        names.append(name)
    rows = data.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise PanelError("compare needs rows: 1 to 8 objects with a label and values")
    body: list[str] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise PanelError(f"compare row {index} must be an object with a label and values")
        label = _clip(row.get("label") or row.get("title") or row.get("name"), 40)
        values = row.get("values")
        if label is None or not isinstance(values, list) or len(values) != len(names):
            raise PanelError(f"compare row {index} needs a label and one value per column")
        cells: list[str] = []
        for value_index, value in enumerate(values, start=1):
            text = _clip(value, 40)
            if text is None:
                raise PanelError(f"compare row {index} value {value_index} is empty")
            cells.append(f"<td>{_esc(text)}</td>")
        body.append(f'<tr><th scope="row">{_esc(label)}</th>{"".join(cells)}</tr>')
    heading = _title(data)
    head = "".join(f"<th>{_esc(name)}</th>" for name in names)
    title_html = f"<h1>{_esc(heading)}</h1>" if heading else ""
    return (
        f"{title_html}"
        f"<table><thead><tr><th></th>{head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
        f"{_buttons(data, 'compare')}"
    )


def _agenda(data: dict[str, Any]) -> str:
    items = data.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        raise PanelError("agenda needs items: 1 to 8 objects with when and title")
    rows: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise PanelError(f"agenda item {index} must be an object with when and title")
        when = _clip(item.get("when") or item.get("time"), 40)
        title = _clip(item.get("title") or item.get("label") or item.get("text"), 80)
        if when is None or title is None:
            raise PanelError(f"agenda item {index} needs when and title")
        detail = _clip(item.get("detail") or item.get("subtitle"), 120)
        extra = f' <span class="dim">{_esc(detail)}</span>' if detail else ""
        rows.append(f"<dt>{_esc(when)}</dt><dd>{_esc(title)}{extra}</dd>")
    heading = _title(data)
    title_html = f"<h1>{_esc(heading)}</h1>" if heading else ""
    return f"{title_html}<dl>{''.join(rows)}</dl>{_buttons(data, 'agenda')}"


def _steps(data: dict[str, Any]) -> str:
    steps = data.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        raise PanelError("steps needs steps: 1 to 8 strings or objects with text")
    rows: list[str] = []
    for index, step in enumerate(steps, start=1):
        if isinstance(step, str):
            step = {"text": step}
        if not isinstance(step, dict):
            raise PanelError(f"step {index} must be text")
        text = _clip(step.get("text") or step.get("title") or step.get("label"), 160)
        if text is None:
            raise PanelError(f"step {index} needs text")
        detail = _clip(step.get("detail"), 120)
        extra = f' <span class="dim">{_esc(detail)}</span>' if detail else ""
        rows.append(f"<li>{_esc(text)}{extra}</li>")
    heading = _title(data)
    title_html = f"<h1>{_esc(heading)}</h1>" if heading else ""
    return f"{title_html}<ol>{''.join(rows)}</ol>{_buttons(data, 'steps')}"


def _fact(data: dict[str, Any]) -> str:
    title = _req(data, "title", 80, "fact title")
    text = _req(data, "text", 320, "fact text")
    caption = _opt(data, "caption", 120)
    source = _opt(data, "source", 80)
    image = data.get("image")
    figure = ""
    if image is not None:
        if not isinstance(image, str) or _DATA_IMAGE_RE.fullmatch(image.strip()) is None:
            raise PanelError("fact image must be a data:image base64 uri, or omit image")
        alt = _clip(data.get("alt") or title, 120) or title
        figure = f'<figure><img alt="{_esc(alt)}" src="{image.strip()}">'
        if caption:
            figure += f'<figcaption class="faint">{_esc(caption)}</figcaption>'
        figure += "</figure>"
    elif caption:
        figure = f'<p class="faint">{_esc(caption)}</p>'
    source_html = f'<p class="faint">{_esc(source)}</p>' if source else ""
    return (
        f"<h1>{_esc(title)}</h1><p>{_esc(text)}</p>{figure}{source_html}{_buttons(data, 'fact')}"
    )


def _side(value: object, where: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise PanelError(f"score {where} must be an object with name and score")
    name = _clip(value.get("name") or value.get("label"), 40)
    score = _clip(value.get("score"), 16)
    if name is None or score is None:
        raise PanelError(f"score {where} needs name and score")
    return name, score


def _score(data: dict[str, Any]) -> str:
    left_name, left_score = _side(data.get("left"), "left")
    right_name, right_score = _side(data.get("right"), "right")
    heading = _title(data) or _opt(data, "status", 40)
    detail = _opt(data, "detail", 160)
    title_html = f"<h1>{_esc(heading)}</h1>" if heading else ""
    extra = f'<p class="dim">{_esc(detail)}</p>' if detail else ""
    return (
        f"{title_html}"
        f'<div class="card row">'
        f'<div><p class="kicker">{_esc(left_name)}</p><p class="stat">{_esc(left_score)}</p></div>'
        f'<div><p class="kicker">{_esc(right_name)}</p><p class="stat">{_esc(right_score)}</p></div>'
        f"</div>{extra}{_buttons(data, 'score')}"
    )


def _chart(data: dict[str, Any]) -> str:
    kind = data.get("type")
    if kind not in ("bar", "line"):
        raise PanelError("chart type must be bar or line")
    labels_raw = data.get("labels")
    values_raw = data.get("values")
    if not isinstance(labels_raw, list) or not isinstance(values_raw, list):
        raise PanelError("chart needs labels and values")
    if not 1 <= len(labels_raw) <= 8 or len(labels_raw) != len(values_raw):
        raise PanelError("chart needs 1 to 8 labels and the same number of values")
    labels: list[str] = []
    values: list[float] = []
    for index, (label, value) in enumerate(zip(labels_raw, values_raw), start=1):
        text = _clip(label, 16)
        if text is None:
            raise PanelError(f"chart label {index} needs text")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise PanelError(f"chart value {index} must be a number")
        labels.append(text)
        values.append(float(value))
    from .panel_images import chart_data_url

    src = chart_data_url(kind, labels, values)
    if src is None:
        raise PanelError("chart could not be drawn")
    heading = _title(data)
    title_html = f"<h1>{_esc(heading)}</h1>" if heading else ""
    alt = heading or ("Bar chart" if kind == "bar" else "Line chart")
    return f'{title_html}<img alt="{_esc(alt)}" src="{src}">{_buttons(data, "chart")}'


def _key_points(data: dict[str, Any]) -> str:
    points = data.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= 8:
        raise PanelError("key_points needs points: 1 to 8 strings")
    items: list[str] = []
    for index, point in enumerate(points, start=1):
        text = _clip(point, 160)
        if text is None:
            raise PanelError(f"key_points point {index} needs text")
        items.append(f"<li>{_esc(text)}</li>")
    heading = _title(data)
    title_html = f"<h1>{_esc(heading)}</h1>" if heading else ""
    return f"{title_html}<ul>{''.join(items)}</ul>{_buttons(data, 'key_points')}"


_RENDERERS = {
    "weather": _weather,
    "compare": _compare,
    "agenda": _agenda,
    "steps": _steps,
    "fact": _fact,
    "score": _score,
    "key_points": _key_points,
    "chart": _chart,
}


def _walk_fragment(html: str) -> None:
    index = 0
    while index < len(html):
        if html[index] != "<":
            index += 1
            continue
        end = _tag_end(html, index)
        _check_tag(html[index + 1 : end])
        index = end + 1


def _tag_end(html: str, start: int) -> int:
    quote = ""
    index = start + 1
    while index < len(html):
        ch = html[index]
        if quote:
            if ch == quote:
                quote = ""
            index += 1
            continue
        if ch in {'"', "'"}:
            quote = ch
            index += 1
            continue
        if ch == "<":
            raise PanelError("tag is not closed")
        if ch == ">":
            return index
        index += 1
    raise PanelError("tag is not closed")


def _check_tag(raw: str) -> None:
    body = raw.strip()
    if body.endswith("/"):
        body = body[:-1].strip()
    closing = body.startswith("/")
    if closing:
        body = body[1:].strip()
    if not body or body[0] in {"!", "?"}:
        raise PanelError("tag is not allowed")
    parts = body.split(None, 1)
    name = parts[0].lower()
    if not name.isalnum() or name not in _ALLOWED_TAGS:
        raise PanelError(f"tag {name} is not allowed")
    if closing:
        if len(parts) > 1:
            raise PanelError("closing tag cannot have attributes")
        return
    attrs = _attributes(parts[1] if len(parts) > 1 else "")
    classes = [value for attr, value in attrs if attr == "class"]
    if name == "a":
        class_text = " ".join(value or "" for value in classes)
        if "btn" not in class_text.split():
            raise PanelError("links must use the btn class")
    for attr, value in attrs:
        _check_attr(name, attr, value)


def _attributes(source: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    index = 0
    while index < len(source):
        while index < len(source) and source[index].isspace():
            index += 1
        if index >= len(source):
            break
        if source[index] == "/":
            index += 1
            continue
        match = _ATTR_RE.match(source, index)
        if match is None:
            raise PanelError("attribute is not allowed")
        name = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3)
        if value is None:
            value = match.group(4)
        out.append((name, value))
        index = match.end()
    return out


def _check_attr(tag: str, name: str, value: str | None) -> None:
    if name.startswith("on") or name == "style":
        raise PanelError("attribute is not allowed")
    if name == "href":
        if tag != "a" or not value or not value.lower().startswith("koji-action:"):
            raise PanelError("href must be a koji-action link")
        if "<" in value or ">" in value:
            raise PanelError("href is not allowed")
        return
    if name == "src":
        if tag != "img" or not value or _DATA_IMAGE_RE.fullmatch(value.strip()) is None:
            raise PanelError("image must be a data uri")
        return
    if name not in _SAFE_ATTRS:
        raise PanelError(f"attribute {name} is not allowed")
    if name == "reversed":
        if value is not None:
            raise PanelError("attribute is not allowed")
        return
    if value is None or "<" in value or ">" in value:
        raise PanelError("attribute is not allowed")
    if name == "class":
        if len(value) > 80 or any(not (ch.isalnum() or ch in " _-") for ch in value):
            raise PanelError("class is not allowed")
