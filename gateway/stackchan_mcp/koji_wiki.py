# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Koji role wiki: Markdown pages on disk, plus Portal presence.

The Grok login-voice path loads ``roles`` and ``handoff`` (and the short
catalog page) from ``gateway/wiki/`` and appends that text the same way other
``ToolExtension`` instructions are appended. It does not add a tool and it
does not add another model.

Portal fetches the same pages over the capture HTTP server:

- ``GET /wiki/manifest``
- ``GET /wiki/pages/{id}``

Default listen address is ``0.0.0.0:8766`` (``HOST`` / ``CAPTURE_PORT``), so
the Portal on the LAN uses ``http://<iMac-LAN-IP>:8766/wiki/manifest``.

Inbound ``{"type":"koji_presence","presence":{...}}`` arrives on the same
XiaoZhi WebSocket as ``type:panel`` and ``type:device_command``. The gateway
writes the last good body to ``wiki/presence/portal.json`` and does not treat
the frame as user speech. A healthy Grok voice session writes
``wiki/presence/bridge.json``. Neither file is source; both are runtime.

``STACKCHAN_KOJI_WIKI=0`` leaves the voice-instruction block out. HTTP and
presence files stay available. ``STACKCHAN_KOJI_WIKI_DIR`` overrides the
wiki root (default is ``gateway/wiki`` next to this package).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

_PAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_REQUIRED_INSTRUCTION_PAGES = ("roles", "handoff")
_MAX_PAGE_BYTES = 64 * 1024
_MAX_INSTRUCTION_CHARS = 32_000
_MAX_PRESENCE_BYTES = 4096
_MAX_STRING = 200

# v1 fields. Unknown keys are rejected so a heartbeat cannot smuggle text
# into the presence file.
PRESENCE_BOOL_FIELDS = (
    "portal_online",
    "bridge_online",
    "voice_turn",
    "spotify_playing",
    "now_playing_visible",
)
PRESENCE_STRING_FIELDS = ("wiki_etag", "ts")
PRESENCE_FIELDS = PRESENCE_BOOL_FIELDS + PRESENCE_STRING_FIELDS

_SUMMARY_BITS = (
    ("portal_online", True, "Portal online"),
    ("portal_online", False, "Portal offline"),
    ("voice_turn", True, "voice turn active"),
    ("spotify_playing", True, "Spotify playing"),
    ("now_playing_visible", True, "now playing visible"),
)


def koji_wiki_enabled() -> bool:
    """Voice instructions include the wiki unless this is explicitly off."""
    raw = os.getenv("STACKCHAN_KOJI_WIKI")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def wiki_root() -> Path:
    """Directory that holds ``manifest.json``, ``pages/``, and ``presence/``."""
    raw = os.getenv("STACKCHAN_KOJI_WIKI_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "wiki"


@dataclass(frozen=True)
class WikiPage:
    page_id: str
    path: str
    text: str
    etag: str


@dataclass(frozen=True)
class WikiBundle:
    version: int
    pages: dict[str, WikiPage]
    order: tuple[str, ...]
    etag: str

    def manifest_document(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "etag": self.etag,
            "pages": [
            {
                "id": self.pages[page_id].page_id,
                "path": self.pages[page_id].path,
                "etag": self.pages[page_id].etag,
            }
            for page_id in self.order
        ],
        }


def _etag(payload: bytes) -> str:
    return '"sha256-' + hashlib.sha256(payload).hexdigest() + '"'


def _read_page(root: Path, relative: str) -> str | None:
    if relative.startswith("/") or "\\" in relative:
        return None
    parts = Path(relative).parts
    if not parts or any(part in {"..", ""} for part in parts):
        return None
    root_resolved = root.resolve()
    path = root_resolved.joinpath(*parts)
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if root_resolved != resolved and root_resolved not in resolved.parents:
        return None
    if not resolved.is_file():
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > _MAX_PAGE_BYTES:
        logger.warning("Koji wiki page is too large: %s", relative)
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logger.warning("Koji wiki page is unreadable: %s", relative)
        return None


def load_bundle(root: Path | None = None) -> WikiBundle | None:
    """Read the manifest and its pages. None when the tree cannot be used."""
    root = wiki_root() if root is None else root
    manifest_path = root / "manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.warning("Koji wiki manifest is unreadable")
        return None
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    entries = raw.get("pages")
    if type(version) is not int or version < 1 or not isinstance(entries, list) or not entries:
        return None
    pages: dict[str, WikiPage] = {}
    order: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        page_id = entry.get("id")
        relative = entry.get("path")
        if not isinstance(page_id, str) or _PAGE_ID_RE.fullmatch(page_id) is None:
            return None
        if page_id in pages or not isinstance(relative, str):
            return None
        text = _read_page(root, relative)
        if text is None:
            return None
        pages[page_id] = WikiPage(
            page_id=page_id,
            path=relative,
            text=text,
            etag=_etag(text.encode("utf-8")),
        )
        order.append(page_id)
    canonical = json.dumps(
        {
            "version": version,
            "pages": [{"id": page_id, "etag": pages[page_id].etag} for page_id in order],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return WikiBundle(version=version, pages=pages, order=tuple(order), etag=_etag(canonical))


def presence_summary(root: Path | None = None) -> str:
    """One line from the last Portal heartbeat. Empty when there is nothing to say."""
    root = wiki_root() if root is None else root
    path = root / "presence" / "portal.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    bits = [label for key, expected, label in _SUMMARY_BITS if data.get(key) is expected]
    if not bits:
        return ""
    return "Presence: " + "; ".join(bits) + "."


def load_instruction_text(root: Path | None = None) -> str:
    """Roles, handoff, and the catalog pointer, loaded from Markdown on disk."""
    if not koji_wiki_enabled():
        return ""
    root = wiki_root() if root is None else root
    bundle = load_bundle(root)
    if bundle is None:
        return ""
    parts = [
        (
            "Koji wiki (roles and handoff). One persona, two execution seats. "
            "This is shared memory, not a second brain. "
            "Portal volume, media, and Spotify stay on device_command."
        )
    ]
    for page_id in _REQUIRED_INSTRUCTION_PAGES:
        page = bundle.pages.get(page_id)
        if page is None or not page.text.strip():
            return ""
        parts.append(page.text.strip())
    catalog = bundle.pages.get("catalog")
    if catalog is not None and catalog.text.strip():
        parts.append(catalog.text.strip())
    summary = presence_summary(root)
    if summary:
        parts.append(summary)
    text = "\n\n".join(parts)
    if len(text) > _MAX_INSTRUCTION_CHARS:
        logger.warning("Koji wiki instructions are too long; leaving them out")
        return ""
    return text


def koji_wiki_extension() -> Any | None:
    """Instruction-only extension. Empty when the flag is off or pages are missing."""
    text = load_instruction_text()
    if not text:
        return None
    from .voice_tools import ToolExtension

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        del name, args
        return {"ok": False, "error": "koji wiki has no tools"}

    return ToolExtension(declarations=(), dispatch=dispatch, instructions=text)


def _presence_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_STRING:
        return None
    if any(ord(ch) < 32 for ch in text):
        return None
    return text


def parse_koji_presence(frame: object) -> dict[str, Any] | None:
    """Return the sanitized presence object, or None when the frame is not stored."""
    if not isinstance(frame, dict) or frame.get("type") != "koji_presence":
        return None
    if set(frame) != {"type", "presence"}:
        return None
    try:
        encoded = json.dumps(frame, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > _MAX_PRESENCE_BYTES:
        return None
    presence = frame.get("presence")
    if not isinstance(presence, dict) or not presence:
        return None
    if set(presence) - set(PRESENCE_FIELDS):
        return None
    cleaned: dict[str, Any] = {}
    for key in PRESENCE_BOOL_FIELDS:
        if key not in presence:
            continue
        value = presence[key]
        if type(value) is not bool:
            return None
        cleaned[key] = value
    for key in PRESENCE_STRING_FIELDS:
        if key not in presence:
            continue
        text = _presence_string(presence[key])
        if text is None:
            return None
        cleaned[key] = text
    if not cleaned:
        return None
    if "ts" not in cleaned:
        cleaned["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return cleaned


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def record_portal_presence(frame: object, root: Path | None = None) -> bool:
    """Persist one Portal heartbeat. Invalid frames are dropped and not raised."""
    cleaned = parse_koji_presence(frame)
    if cleaned is None:
        return False
    root = wiki_root() if root is None else root
    try:
        _write_json(root / "presence" / "portal.json", cleaned)
    except OSError:
        logger.warning("Koji portal presence could not be saved")
        return False
    return True


def publish_bridge_presence(*, online: bool, root: Path | None = None) -> bool:
    """Write ``presence/bridge.json`` for a healthy or dropped Grok voice session."""
    root = wiki_root() if root is None else root
    payload: dict[str, Any] = {
        "bridge_online": bool(online),
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    bundle = load_bundle(root)
    if bundle is not None:
        payload["wiki_etag"] = bundle.etag
    try:
        _write_json(root / "presence" / "bridge.json", payload)
    except OSError:
        logger.warning("Koji bridge presence could not be saved")
        return False
    return True


def _unavailable() -> web.Response:
    return web.json_response({"error": "wiki unavailable"}, status=404)


async def handle_wiki_manifest(_request: web.Request) -> web.Response:
    """Read-only manifest. Etags are computed from the page bytes."""
    bundle = load_bundle()
    if bundle is None:
        return _unavailable()
    return web.json_response(bundle.manifest_document(), headers={"ETag": bundle.etag})


async def handle_wiki_page(request: web.Request) -> web.Response:
    """Read-only Markdown for one manifest page id."""
    page_id = request.match_info.get("page_id", "")
    if not isinstance(page_id, str) or _PAGE_ID_RE.fullmatch(page_id) is None:
        return web.json_response({"error": "unknown page"}, status=404)
    bundle = load_bundle()
    if bundle is None:
        return _unavailable()
    page = bundle.pages.get(page_id)
    if page is None:
        return web.json_response({"error": "unknown page"}, status=404)
    return web.Response(
        text=page.text,
        content_type="text/markdown",
        charset="utf-8",
        headers={"ETag": page.etag},
    )
