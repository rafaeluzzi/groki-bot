# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Thin XiaoZhi ``type:panel`` frames for the Portal screen.

The Grok login-voice path declares these tools locally and forwards a call as
one JSON text frame on the device WebSocket. The frame does not use MCP, so
it still works while hello advertises ``features.mcp: false``.

Tool names (any of these, case-insensitive; dots become underscores):

| Call name | Action | Frame |
|---|---|---|
| ``show_panel``, ``panel_show``, ``self.panel.show`` | show | ``{"type":"panel","panel":{...}}`` |
| ``dismiss_panel``, ``panel_dismiss``, ``self.panel.dismiss`` | dismiss | ``{"type":"panel","dismiss":true}`` |

``show_panel`` takes the panel document as a JSON object or as a JSON string
of that object. The gateway rebuilds it into the Koji version 1 schema
(``android/docs/compose-panel-kit.md`` in grok-portal) before the frame is
sent, because Koji drops a document it cannot parse and never says so:

- ``version`` may be omitted; any other value than ``1`` is rejected.
- ``id`` and row/action ids are filled in from the titles when missing.
- ``list`` takes ``rows`` or ``items``; an item may be a string or an object
  with ``label``, ``title``, ``text``, or ``name``. At most 12 rows are sent.
- ``now_playing`` takes ``title`` (or ``track``/``name``/``song``) and
  ``show`` (or ``artist``/``podcast``/``album``); ``primary`` defaults to
  Pause, or Play when ``paused`` is true.
- ``choice`` takes ``prompt`` (or ``title``/``text``) and 1 to 4 ``actions``
  (or ``options``/``choices``).
- ``webview`` (``html`` is an alias) takes an ``html`` fragment, or
  ``template`` plus ``data``. Koji does not load URLs. Templates render to a
  fragment that stays inside the sanitizer. See ``panel_templates.py``.
- Optional ``ttl_s`` is whole seconds the card stays up. ``0`` stays until
  dismissed. Omitted uses the kind default on the device (list 45, choice 60,
  webview 30). ``now_playing`` ignores ``ttl_s`` and does not time out.

Text over Koji's length caps is cut with an ellipsis, and ``<`` in plain text
becomes ``‹``. Unknown fields are dropped. A rejected document returns a
reason the model can act on.

Portal does not ack ``type:panel`` today (unlike ``type:core``). The tool
returns ok after the document is validated and the frame is written. That
write does not send ``abort`` and does not change the voice session. Inbound
``type:panel`` text is not user speech and is not forwarded into the voice
turn. A device ``panel_action`` (a tap that is not ``dismiss`` or ``local:``)
is injected as one short user turn when a Grok voice session is live. With
no session the gateway logs ``dropped(no_session)`` and does not open one.

Set ``STACKCHAN_PANEL_FRAMES=0`` to stop declaring the tools on the gbot path.
The default is on, same as ``STACKCHAN_CORE_FRAMES``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

PANEL_KINDS = ("list", "now_playing", "choice", "webview")
_KIND_ALIASES = {"html": "webview"}

# Normalized spellings the voice model or a session catalog might use.
_NORMALIZED_ACTIONS = {
    "show_panel": "show",
    "panel_show": "show",
    "self_panel_show": "show",
    "dismiss_panel": "dismiss",
    "panel_dismiss": "dismiss",
    "self_panel_dismiss": "dismiss",
}

# Declared on the gbot session. Dotted names are sanitized to underscores on
# the provider wire (``self.panel.show`` -> ``self_panel_show``) and dispatched
# back to the original name. Do not also declare the sanitized spelling: that
# collides with the dotted form.
PANEL_DECLARED_NAMES = (
    "show_panel",
    "self.panel.show",
    "dismiss_panel",
    "self.panel.dismiss",
)

_CANONICAL = {
    "show": "show_panel",
    "dismiss": "dismiss_panel",
}

MAX_PANEL_BYTES = 65536
_MAX_DEPTH = 8
_MAX_INPUT_ITEMS = 40
_MAX_KEYS = 48
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

# Koji limits, from PanelJson.kt / compose-panel-kit.md.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_MAX_ROWS = 12
_MAX_ACTIONS = 4
_MAX_TITLE = 80
_MAX_SUBTITLE = 120
_MAX_PROMPT = 160
_MAX_ACTION_LABEL = 40
_MAX_URL = 500
_MAX_HTML = 48000
_MAX_SECONDS = 172_800
_MAX_TTL_S = 600

# Device applies these when the document omits ttl_s. now_playing never times out.
DEFAULT_TTL_S = {
    "list": 45,
    "choice": 60,
    "webview": 30,
    "now_playing": 0,
}

_ROW_LABEL_KEYS = ("title", "label", "text", "name")
_ROW_SUBTITLE_KEYS = ("subtitle", "description", "detail")
_NOW_PLAYING_TITLE_KEYS = ("title", "track", "name", "song", "episode")
_NOW_PLAYING_SHOW_KEYS = ("show", "artist", "podcast", "album", "subtitle")
_CHOICE_PROMPT_KEYS = ("prompt", "title", "text", "question")
_ACTION_LABEL_KEYS = ("label", "title", "text", "name")
_ART_KEYS = ("artUrl", "art_url", "image", "imageUrl", "image_url")
_HTML_KEYS = ("html", "document", "markup", "content")
_URL_KEYS = ("url", "src", "href")

PANEL_EXAMPLE = (
    '{"version":1,"kind":"list","title":"Podcasts I follow",'
    '"items":[{"title":"All-In"},{"title":"Planet Money"}]}'
)

_SHOW_DESCRIPTION = (
    "Show a panel beside the orb on the connected Portal. "
    "panel is a JSON object (or JSON string) with version 1 and a kind. Example: "
    + PANEL_EXAMPLE
    + ". kind list: title and items, each a string or an object with title, label, or text "
    "(optional subtitle); at most 12 are shown. "
    "kind now_playing: title and show (or artist). "
    "kind choice: prompt and 1 to 4 options, each with a label. "
    "kind webview: prefer template and data over raw html. "
    "Templates: weather, compare, agenda, steps, fact, score, key_points, chart. "
    'Example: {"kind":"webview","template":"weather","data":{"place":"Durham","temp":"72°","condition":"Mild"}}. '
    'chart: {"kind":"webview","template":"chart","data":{"title":"Week","type":"bar","labels":["Mon","Tue"],"values":[1,3]}}. '
    "An https image on a list item (image or art_url) or on fact data.image is fetched and shown small. "
    "A picture that cannot be fetched is left off; the card still shows. "
    "Optional ttl_s is seconds (0 stays until dismissed). now_playing does not time out. "
    "A new panel replaces the open one. The same id updates in place. "
    "Use list for places or rows, choice for 2 to 4 options, a template for a card or table, now_playing for music. "
    "When the person is picking among 2 to 4 options, call kind choice in that same turn. "
    'Example: {"version":1,"kind":"choice","prompt":"Podcasts or Spotify?","options":["Podcasts","Spotify"]}. '
    "This does not speak audio and does not replace the spoken reply. "
    "ok means the frame was accepted and sent. On an error, fix what the error names."
)

_DISMISS_DESCRIPTION = (
    "Close the panel on the connected Portal screen. "
    "This does not speak audio. ok means the dismiss frame was sent."
)

PANEL_INSTRUCTIONS = (
    "A panel sits beside the orb and takes about two thirds of the screen. "
    "When a list, what is playing, or a short choice fits, call show_panel with a "
    "version 1 panel document of kind list, now_playing, or choice, for example "
    + PANEL_EXAMPLE
    + ". self.panel.show is the same action. "
    "dismiss_panel and self.panel.dismiss close it. "
    "For a rich card, use kind webview with template and data "
    "(weather, compare, agenda, steps, fact, score, key_points, chart) instead of raw html. "
    "chart takes type bar or line, labels, and values. "
    "Pass an https image on a list row or a fact when a picture helps. "
    "When the person is picking among 2 to 4 options, call show_panel with kind choice "
    "in that same turn and speak one short line. "
    'Example: {"version":1,"kind":"choice","prompt":"Podcasts or Spotify?","options":["Podcasts","Spotify"]}. '
    "A new panel replaces the open one. The same id updates in place. "
    "ttl_s is optional seconds; 0 stays until dismissed. "
    "A button tap arrives as a user turn. "
    "A list titled Podcasts I follow is shown A-to-Z by title, the same order as the numbered followed shows. "
    "Skip a blank title. If the podcast tool replaced it with a feed title, use that title in this order. "
    "number N counts from the top of the titled rows. "
    "Panels do not replace your spoken reply. "
    "Do not claim the panel showed before the tool returns ok. "
    "Do not end every reply with an open-ended follow-up."
)


def panel_frames_enabled() -> bool:
    """Grok voice declares Portal panel tools unless this is explicitly off."""
    raw = os.getenv("STACKCHAN_PANEL_FRAMES")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_panel_tool_name(name: str) -> str:
    lowered = name.strip().lower().replace(".", "_")
    collapsed = re.sub(r"[^a-z0-9_]+", "_", lowered)
    return re.sub(r"_+", "_", collapsed).strip("_")


def panel_action_for_tool(name: object) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return _NORMALIZED_ACTIONS.get(normalize_panel_tool_name(name))


def canonical_panel_tool(name: object) -> str | None:
    action = panel_action_for_tool(name)
    if action is None:
        return None
    return _CANONICAL[action]


class PanelError(ValueError):
    """A show_panel document Koji would not render. The message names the fix.

    Messages never echo document text, so a rejected panel does not leak its
    contents into logs or the tool result.
    """


def _key_ok(key: object) -> bool:
    return isinstance(key, str) and _KEY_RE.fullmatch(key) is not None


def _walk(value: object, depth: int) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return "\x00" not in value
    if isinstance(value, list):
        return len(value) <= _MAX_INPUT_ITEMS and all(_walk(item, depth + 1) for item in value)
    if isinstance(value, dict):
        if len(value) > _MAX_KEYS:
            return False
        return all(_key_ok(key) and _walk(item, depth + 1) for key, item in value.items())
    return False


def _token(raw: str, fallback: str) -> str:
    """Same rewrite as Koji ``panelToken``: ``ep 1`` becomes ``ep-1``."""
    out: list[str] = []
    dash = False
    for ch in raw.strip():
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


def _id(value: object, fallback: str) -> str:
    if type(value) is int and value >= 0:
        value = str(value)
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if _TOKEN_RE.fullmatch(text):
        return text
    return _token(text, fallback)


def _unique(ident: str, seen: set[str], index: int) -> str:
    candidate = ident
    if candidate in seen:
        candidate = f"{ident[:70]}-{index}"
    while candidate in seen:
        index += 1000
        candidate = f"{ident[:70]}-{index}"
    seen.add(candidate)
    return candidate


def _plain(value: object, limit: int) -> str | None:
    """Plain text Koji accepts: no markup, no control characters, under the cap."""
    if not isinstance(value, str):
        return None
    text = "".join(" " if ord(ch) < 32 else ch for ch in value).strip()
    text = text.replace("<", "\u2039")
    if not text:
        return None
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def _first(source: dict[str, Any], keys: tuple[str, ...], limit: int) -> tuple[str | None, str | None]:
    """First usable text among ``keys`` and the key it came from."""
    for key in keys:
        text = _plain(source.get(key), limit)
        if text is not None:
            return text, key
    return None, None


def _art(source: dict[str, Any]) -> str | None:
    from .panel_images import data_image_ok, http_image_ok

    for key in _ART_KEYS:
        value = source.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if data_image_ok(text) or http_image_ok(text, limit=_MAX_URL):
            return text
    return None


def _action(value: object, fallback: str, *, where: str) -> dict[str, str]:
    if isinstance(value, str):
        value = {"label": value}
    if not isinstance(value, dict):
        raise PanelError(f"{where} must be an object with a label")
    label, _ = _first(value, _ACTION_LABEL_KEYS, _MAX_ACTION_LABEL)
    if label is None:
        raise PanelError(f"{where} needs a label")
    ident = _id(value.get("id"), _token(label, fallback))
    return {"id": ident, "label": label}


def _seconds(value: object, key: str, *, scale: int = 1) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PanelError(f"{key} must be a number")
    seconds = int(value // scale) if scale > 1 else value
    if seconds != int(seconds) or not 0 <= seconds <= _MAX_SECONDS:
        raise PanelError(f"{key} must be whole seconds from 0 to {_MAX_SECONDS}")
    return int(seconds)


def _list(source: dict[str, Any], doc: dict[str, Any]) -> int:
    rows_in = source.get("rows", source.get("items"))
    if not isinstance(rows_in, list) or not rows_in:
        raise PanelError("kind list needs items: a non-empty array of strings or objects with a title")
    title, _ = _first(source, ("title", "heading", "name"), _MAX_TITLE)
    doc["title"] = title or "List"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(rows_in[:_MAX_ROWS], start=1):
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            raise PanelError(f"list item {index} must be a string or an object with a title")
        label, used = _first(item, _ROW_LABEL_KEYS, _MAX_TITLE)
        if label is None:
            raise PanelError(f"list item {index} needs a title, label, or text")
        row: dict[str, Any] = {
            "id": _unique(_id(item.get("id"), f"row-{index}"), seen, index),
            "label": label,
        }
        subtitle, _ = _first(item, _ROW_SUBTITLE_KEYS, _MAX_SUBTITLE)
        if subtitle is None:
            rest = tuple(key for key in _ROW_LABEL_KEYS if key != used)
            other, _ = _first(item, rest, _MAX_SUBTITLE)
            if other is not None and other != label:
                subtitle = other
        if subtitle is not None:
            row["subtitle"] = subtitle
        if item.get("action") is not None:
            row["action"] = _action(item["action"], f"action-{index}", where=f"list item {index} action")
        art = _art(item)
        if art is not None:
            row["artUrl"] = art
        rows.append(row)
    if _followed_panel(str(doc.get("title") or "")):
        rows.sort(key=lambda row: (not row["label"].strip(), row["label"].lower()))
    doc["rows"] = rows
    return max(0, len(rows_in) - _MAX_ROWS)


def _followed_panel(title: str) -> bool:
    """The followed-podcast list. Row order matches ``GET /api/podcasts/followed``."""
    return " ".join(title.lower().split()) == "podcasts i follow"


def _now_playing(source: dict[str, Any], doc: dict[str, Any]) -> int:
    title, _ = _first(source, _NOW_PLAYING_TITLE_KEYS, _MAX_TITLE)
    if title is None:
        raise PanelError("kind now_playing needs a title")
    show, _ = _first(source, _NOW_PLAYING_SHOW_KEYS, _MAX_TITLE)
    doc["title"] = title
    doc["show"] = show or "Now playing"
    art = _art(source)
    if art is not None:
        doc["artUrl"] = art
    progress = source.get("progress")
    if progress is not None:
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= progress <= 1:
            raise PanelError("progress must be a number from 0 to 1")
        doc["progress"] = progress
    elapsed = _seconds(source.get("elapsed"), "elapsed")
    if elapsed is None:
        elapsed = _seconds(source.get("position_ms"), "position_ms", scale=1000)
    duration = _seconds(source.get("duration"), "duration")
    if duration is None:
        duration = _seconds(source.get("duration_ms"), "duration_ms", scale=1000)
    if elapsed is not None:
        doc["elapsed"] = elapsed
    if duration is not None:
        if duration < 1:
            raise PanelError("duration must be at least 1 second")
        doc["duration"] = duration
    if source.get("primary") is not None:
        doc["primary"] = _action(source["primary"], "primary", where="primary")
    elif source.get("paused") is True:
        doc["primary"] = {"id": "local:resume", "label": "Play"}
    else:
        doc["primary"] = {"id": "local:pause", "label": "Pause"}
    if source.get("secondary") is not None:
        doc["secondary"] = _action(source["secondary"], "secondary", where="secondary")
    return 0


def _choice(source: dict[str, Any], doc: dict[str, Any]) -> int:
    prompt, _ = _first(source, _CHOICE_PROMPT_KEYS, _MAX_PROMPT)
    if prompt is None:
        raise PanelError("kind choice needs a prompt")
    options = next(
        (source[key] for key in ("actions", "options", "choices") if source.get(key) is not None),
        None,
    )
    if not isinstance(options, list) or not 1 <= len(options) <= _MAX_ACTIONS:
        raise PanelError(f"kind choice needs 1 to {_MAX_ACTIONS} options, each with a label")
    actions: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, option in enumerate(options, start=1):
        action = _action(option, f"option-{index}", where=f"option {index}")
        action["id"] = _unique(action["id"], seen, index)
        actions.append(action)
    doc["prompt"] = prompt
    doc["actions"] = actions
    return 0


def _webview(source: dict[str, Any], doc: dict[str, Any]) -> int:
    from .panel_templates import TEMPLATE_NAMES

    template = source.get("template")
    has_template = isinstance(template, str) and bool(template.strip())
    html = next((source[key] for key in _HTML_KEYS if isinstance(source.get(key), str) and source.get(key).strip()), None)
    # A card we already built carries the template name beside the fragment so
    # the device can log it. The model still may not send html and data together.
    if has_template and html is not None and "data" not in source:
        name = template.strip()
        if name not in TEMPLATE_NAMES:
            allowed = ", ".join(TEMPLATE_NAMES)
            raise PanelError(f"template must be one of: {allowed}")
        if len(html) > _MAX_HTML:
            raise PanelError(f"html is longer than {_MAX_HTML} characters")
        doc["template"] = name
        title, _ = _first(source, ("title",), _MAX_TITLE)
        if title is not None:
            doc["title"] = title
        doc["html"] = html
        return 0
    if has_template and html is not None:
        raise PanelError("kind webview takes template and data, or html, not both")
    if has_template:
        from .panel_templates import render_panel_template

        data = source.get("data", {})
        if "data" in source and not isinstance(data, dict):
            raise PanelError("template data must be an object")
        html = render_panel_template(template.strip(), data if isinstance(data, dict) else {})
        if not isinstance(source.get("title"), str):
            label = None
            if isinstance(data, dict):
                label, _ = _first(data, ("title", "place"), _MAX_TITLE)
            if label is not None:
                doc["title"] = label
    if html is None or not str(html).strip():
        if any(isinstance(source.get(key), str) for key in _URL_KEYS):
            raise PanelError("kind webview cannot load a url; send an html fragment in html, or a template and data")
        raise PanelError("kind webview needs an html fragment in html, or a template and data")
    if len(html) > _MAX_HTML:
        raise PanelError(f"html is longer than {_MAX_HTML} characters")
    title, _ = _first(source, ("title",), _MAX_TITLE)
    if title is not None:
        doc["title"] = title
    if has_template:
        doc["template"] = template.strip()
    doc["html"] = html
    return 0


def _ttl(source: dict[str, Any], kind: str) -> int | None:
    """Seconds to put on the document, or None to omit the field.

    ``now_playing`` never times out, so a passed ``ttl_s`` is ignored.
    """
    if "ttl_s" not in source:
        return None
    if kind == "now_playing":
        return None
    value = source.get("ttl_s")
    if isinstance(value, bool) or type(value) is not int or not 0 <= value <= _MAX_TTL_S:
        raise PanelError(f"ttl_s must be a whole number of seconds from 0 to {_MAX_TTL_S}, or omit it")
    return value


_KIND_BUILDERS = {
    "list": _list,
    "now_playing": _now_playing,
    "choice": _choice,
    "webview": _webview,
}


def _coerce_document(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise PanelError("panel is empty")
        if len(text.encode("utf-8")) > MAX_PANEL_BYTES:
            raise PanelError("panel is too large")
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise PanelError("panel string is not valid JSON") from None
    if not isinstance(value, dict):
        raise PanelError("panel must be a JSON object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise PanelError("panel must be plain JSON") from None
    if len(encoded.encode("utf-8")) > MAX_PANEL_BYTES:
        raise PanelError("panel is too large")
    parsed = json.loads(encoded)
    if not _walk(parsed, 1):
        raise PanelError("panel has an unsupported key, is nested too deep, or has over 40 items")
    return parsed


def normalize_panel_document(value: object) -> tuple[dict[str, Any], int]:
    """Rebuild a panel into the Koji version 1 schema.

    Returns the document and how many list rows were left off. Raises
    ``PanelError`` with a short fix when Koji could not show it.
    """
    source = _coerce_document(value)
    version = source.get("version", 1)
    if type(version) is not int or version != 1:
        raise PanelError("version must be the number 1")
    kind = source.get("kind")
    kind = _KIND_ALIASES.get(kind, kind) if isinstance(kind, str) else kind
    if kind not in _KIND_BUILDERS:
        raise PanelError("kind must be list, now_playing, choice, or webview")
    doc: dict[str, Any] = {"version": 1, "kind": kind, "id": ""}
    dropped = _KIND_BUILDERS[kind](source, doc)
    ttl = _ttl(source, kind)
    if ttl is not None:
        doc["ttl_s"] = ttl
    label = doc.get("title") or doc.get("prompt") or kind
    doc["id"] = _id(source.get("id"), _token(label, kind.replace("_", "-")))
    return doc, dropped


def validate_panel_document(value: object) -> dict[str, Any] | None:
    """Return the Koji version 1 copy of a panel document, or None."""
    try:
        return normalize_panel_document(value)[0]
    except PanelError:
        return None


def panel_frame_or_error(tool_name: object, arguments: object) -> tuple[dict[str, Any] | None, str | None, int]:
    """Return ``(frame, None, dropped)`` or ``(None, reason, 0)``."""
    action = panel_action_for_tool(tool_name)
    if action is None:
        return None, "not a panel tool", 0
    if not isinstance(arguments, dict):
        return None, "arguments must be an object", 0
    if action == "dismiss":
        if arguments:
            return None, "dismiss_panel takes no arguments", 0
        return {"type": "panel", "dismiss": True}, None, 0
    if set(arguments) != {"panel"}:
        return None, "pass exactly one argument, panel", 0
    try:
        document, dropped = normalize_panel_document(arguments.get("panel"))
    except PanelError as exc:
        return None, str(exc), 0
    return {"type": "panel", "panel": document}, None, dropped


def build_panel_frame(tool_name: object, arguments: object) -> dict[str, Any] | None:
    """Return the device frame, or None when the call cannot be sent."""
    return panel_frame_or_error(tool_name, arguments)[0]


def is_panel_frame(frame: object) -> bool:
    if not isinstance(frame, dict) or frame.get("type") != "panel":
        return False
    if set(frame) == {"type", "dismiss"}:
        return frame.get("dismiss") is True
    if set(frame) != {"type", "panel"}:
        return False
    document = validate_panel_document(frame.get("panel"))
    return document is not None and document == frame.get("panel")


def panel_tool_parameters(name: str) -> dict[str, Any]:
    action = panel_action_for_tool(name)
    if action is None:
        raise ValueError("not a panel tool")
    if action == "dismiss":
        return {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "panel": {
                "description": _SHOW_DESCRIPTION,
                "anyOf": [{"type": "object"}, {"type": "string"}],
            },
        },
        "required": ["panel"],
        "additionalProperties": False,
    }


def template_name_of(value: object) -> str | None:
    """The template on a show_panel argument, if the model named one."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    name = value.get("template")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def panel_action_delivery(frame: object, *, session_live: bool) -> tuple[str | None, str]:
    """Turn a device tap into one user sentence, or a drop reason.

    Local ids (``dismiss``, ``local:``) are not a voice turn. With no live
    session the reason is ``no_session`` and nothing is opened.
    """
    if not isinstance(frame, dict) or frame.get("type") != "panel_action":
        return None, "not_panel_action"
    panel = frame.get("id")
    action = frame.get("action")
    if not isinstance(panel, str) or _TOKEN_RE.fullmatch(panel) is None:
        return None, "bad_panel"
    if not isinstance(action, str) or _TOKEN_RE.fullmatch(action) is None:
        return None, "bad_action"
    if action == "dismiss" or action.startswith("local:"):
        return None, "local"
    label = frame.get("label", action)
    if not isinstance(label, str):
        return None, "bad_label"
    label = " ".join(label.split())
    if not label:
        label = action
    if len(label) > _MAX_ACTION_LABEL:
        label = label[: _MAX_ACTION_LABEL - 1].rstrip() + "\u2026"
    label = label.replace('"', "'")
    if not session_live:
        return None, "no_session"
    return f'User tapped "{label}" on panel "{panel}".', "delivered"


def panel_tool_description(name: str) -> str:
    action = panel_action_for_tool(name)
    if action is None:
        raise ValueError("not a panel tool")
    canonical = _CANONICAL[action]
    text = _DISMISS_DESCRIPTION if action == "dismiss" else _SHOW_DESCRIPTION
    if name != canonical:
        return f"Alias of {canonical}. " + text
    return text


def panel_tool_declarations() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": panel_tool_description(name),
            "parameters": panel_tool_parameters(name),
        }
        for name in PANEL_DECLARED_NAMES
    ]


def measure_panel(document: dict[str, Any]) -> tuple[str, str, str, int, int, int]:
    """id, kind, template, item count, image count, and JSON byte size.

    The template is ``-`` when the card has none. Image bytes are counted in
    ``bytes`` and are not returned on their own.
    """
    ident = document.get("id")
    ident = ident if isinstance(ident, str) and ident else "-"
    kind = document.get("kind")
    kind = kind if isinstance(kind, str) and kind else "-"
    template = document.get("template")
    template = template if isinstance(template, str) and template else "-"
    if kind == "list":
        rows = document.get("rows") if isinstance(document.get("rows"), list) else []
        items = len(rows)
        images = sum(1 for row in rows if isinstance(row, dict) and row.get("artUrl"))
    elif kind == "choice":
        actions = document.get("actions") if isinstance(document.get("actions"), list) else []
        items = len(actions)
        images = 0
    elif kind == "now_playing":
        items = 1
        images = 1 if document.get("artUrl") else 0
    else:
        items = 1
        html = document.get("html") if isinstance(document.get("html"), str) else ""
        images = html.lower().count("<img")
    size = len(json.dumps(document, ensure_ascii=False).encode("utf-8"))
    return ident, kind, template, items, images, size


def panel_frame_extension(esp32: Any) -> Any:
    """Voice-tool extension that validates a panel and sends one frame.

    Ok means the frame was written. Portal does not ack ``type:panel``.
    Https pictures are fetched and inlined before the frame is built. A
    picture that fails is omitted and the card is still sent.
    """
    from .panel_images import cap_panel_payload, inline_panel_argument
    from .voice_tools import ToolExtension, ToolMetadata

    declarations = tuple(panel_tool_declarations())
    open_id = ["-"]

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        action = panel_action_for_tool(name)
        if action == "show" and isinstance(args, dict) and set(args) == {"panel"}:
            try:
                panel = await inline_panel_argument(args.get("panel"))
            except Exception:
                panel = args.get("panel")
            args = {"panel": panel}
        frame, reason, dropped = panel_frame_or_error(name, args)
        if frame is None:
            return {"ok": False, "error": f"invalid panel arguments: {reason}", "example": PANEL_EXAMPLE}
        if frame.get("dismiss") is not True:
            frame = {"type": "panel", "panel": cap_panel_payload(frame["panel"])}
        send = getattr(esp32, "send_panel", None)
        if not callable(send):
            return {"ok": False, "error": "panel channel unavailable"}
        try:
            result, error = await send(frame)
        except Exception:
            return {"ok": False, "error": "panel frame failed"}
        if error:
            message = "panel frame failed"
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
            return {"ok": False, "error": message}
        if isinstance(result, dict) and result.get("ok") is True:
            if frame.get("dismiss") is True:
                logger.info("panel outcome id=%s result=dismissed-tool", open_id[0])
                open_id[0] = "-"
                return {"ok": True, "dismiss": True}
            document = frame["panel"]
            ident, kind, template, items, images, size = measure_panel(document)
            logger.info(
                "panel shown id=%s kind=%s template=%s items=%s images=%s bytes=%s",
                ident,
                kind,
                template,
                items,
                images,
                size,
            )
            open_id[0] = ident
            shown: dict[str, Any] = {"ok": True, "kind": kind}
            if template != "-" and kind == "webview":
                shown["template"] = template
            if dropped:
                shown["not_shown"] = dropped
            return shown
        return {"ok": False, "error": "panel frame rejected"}

    return ToolExtension(
        declarations=declarations,
        dispatch=dispatch,
        metadata={
            name: ToolMetadata(True, "portal_panel", 5.0) for name in PANEL_DECLARED_NAMES
        },
        instructions=PANEL_INSTRUCTIONS,
    )
