# SPDX-FileCopyrightText: 2026 sefuzhou770801-hub
# SPDX-License-Identifier: MIT
"""Local HTTP forwarding service in front of the ``gbot`` CLI (Grok Bot).

Run it with ``uv run stackchan-gbot-proxy`` on the computer where the Grok Bot
app is signed in. It binds 127.0.0.1 only and never reads or writes a token;
``gbot`` uses the app session.

    POST /send  {"text": "...", "target": "<bot name>"}  -> gbot --json send
    POST /ask   same as /send (older clients)
    GET  /health, /rtt

First sentence: the send runs in the background while the thread is polled;
the first complete short sentence is returned as soon as it appears.
With ``X-Stackchan-Stream: 1`` the response is NDJSON and keeps following the
same thread: every later bot message is sent in full once its text stops
changing (``event: "more"``), then ``event: "done"``. Seen entry ids and
timestamps keep old messages from replaying; time and count limits keep a
request from hanging.

The gateway's ``ask_grokbot`` voice tool (see ``gbot_brain.py``) is the
client. Configuration is read from the environment and from ``.env`` in the
gateway directory; see ``.env.example``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Tests set STACKCHAN_SKIP_DOTENV so a developer .env cannot change the bot under test.
if os.getenv("STACKCHAN_SKIP_DOTENV") != "1":
    try:  # Same .env as the gateway, so one file configures both processes.
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:  # pragma: no cover - python-dotenv is a core dependency
        pass

from .gbot_client import (
    GrokBotClient,
    extract_bot_replies,
    gbot_missing_error,
    resolve_gbot_bin,
    send_timeout_s,
)

# Loopback only, on purpose: anything that can reach this port can message
# your Grok Bot agents as you.
HOST = "127.0.0.1"
PORT = int(os.getenv("STACKCHAN_GBOT_HTTP_PORT", "18770"))
# Bot used when a request names no target. The gateway always sends one
# (STACKCHAN_TOOL_BOT), so this is only a fallback for manual curl tests.
DEFAULT_BOT = (os.getenv("STACKCHAN_ASK_BOT") or os.getenv("STACKCHAN_TOOL_BOT") or "").strip()
POLL_S = float(os.getenv("STACKCHAN_GBOT_POLL_S", "0.10"))
STABLE_S = float(os.getenv("STACKCHAN_GBOT_STABLE_S", "0.35"))
FOLLOW_HARD_S = float(os.getenv("STACKCHAN_GBOT_FOLLOW_S", "110"))
FOLLOW_IDLE_S = float(os.getenv("STACKCHAN_GBOT_FOLLOW_IDLE_S", "90"))
FOLLOW_BUSY_IDLE_S = float(os.getenv("STACKCHAN_GBOT_FOLLOW_BUSY_IDLE_S", "100"))
FOLLOW_MAX = int(os.getenv("STACKCHAN_GBOT_FOLLOW_MAX", "4"))
FIRST_TIMEOUT_S = float(os.getenv("STACKCHAN_GBOT_FIRST_TIMEOUT", "30"))
SEND_TIMEOUT_S = send_timeout_s()
_BUSY_TOKENS = (
    "派", "调研", "我去", "去翻", "去查", "去搜", "稍等", "等我",
    "正在", "交给", "助手", "马上", "先去", "帮你看", "翻一下",
    "去看看", "去问", "先说",
)

_SENT_RE = re.compile(r"(?s)(.+?(?:[。！？!?]+|\n+))")
_TRAIL_OPEN = ("，", ",", "、", "：", ":", "；", ";")
_bot_locks_guard = threading.Lock()
_bot_locks: dict[str, threading.Lock] = {}


def _lock_for_bot(bot: str) -> threading.Lock:
    """Serialise /send per bot so concurrent snapshots cannot match a reply to the wrong request."""
    with _bot_locks_guard:
        lock = _bot_locks.get(bot)
        if lock is None:
            lock = threading.Lock()
            _bot_locks[bot] = lock
        return lock


def first_short_sentence(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    match = _SENT_RE.match(raw)
    if match:
        piece = match.group(1).strip()
        compact = re.sub(r"\s+", "", piece)
        if len(compact) >= 2:
            return piece
    return ""


def ready_utterance(body: str, *, unchanged_s: float) -> str:
    """A complete short sentence, or a short sentence whose text has stopped changing (no full stop needed)."""
    sent = first_short_sentence(body)
    if sent:
        return sent
    compact = re.sub(r"\s+", "", (body or "").strip())
    if (
        2 <= len(compact) <= 28
        and unchanged_s >= STABLE_S
        and not (body or "").strip().endswith(_TRAIL_OPEN)
    ):
        return (body or "").strip()
    return ""


def looks_busy(text: str) -> bool:
    raw = text or ""
    return any(token in raw for token in _BUSY_TOKENS)


def extract_bot_replies_meta(thread: dict) -> list[tuple[str, str, int]]:
    """send-message entries as (id, text, timestampMs). Old messages are filtered out by id and timestamp."""
    payload = thread.get("transcript") or thread.get("thread") or thread
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    out: list[tuple[str, str, int]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "send-message":
            continue
        eid = str(entry.get("id") or "")
        message = entry.get("message")
        text = ""
        if isinstance(message, dict):
            text = str(message.get("content") or "")
        elif isinstance(entry.get("content"), str):
            text = entry["content"]
        ts = entry.get("timestampMs") or entry.get("timestamp") or 0
        try:
            ts_i = int(ts)
        except (TypeError, ValueError):
            ts_i = 0
        out.append((eid, text.strip(), ts_i))
    return out


def _run_gbot_send(text: str, dest: str) -> tuple[bool, str]:
    """Run gbot --json send. Success only means the CLI sent the message."""
    bin_path = resolve_gbot_bin()
    if not bin_path:
        return False, gbot_missing_error()
    try:
        proc = subprocess.run(
            [bin_path, "--json", "send", dest, text],
            capture_output=True,
            text=True,
            timeout=SEND_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "gbot send timeout"
    except OSError as exc:
        return False, f"gbot could not run: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"gbot exit {proc.returncode}")[:400]
        return False, err
    return True, (proc.stdout or "").strip()


def _extract_stdout_reply(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            for key in ("reply", "text", "content", "message"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return ""


class Handler(BaseHTTPRequestHandler):
    _client: GrokBotClient | None = None
    _stream_started: bool = False

    @property
    def client(self) -> GrokBotClient:
        if Handler._client is None:
            Handler._client = GrokBotClient()
        return Handler._client

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _begin_ndjson(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self._stream_started = True

    def _ndjson(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        self.wfile.write(line)
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/rtt"}:
            self._json(200, {"ok": True, "service": "gbot-http", "listen": PORT})
            return
        if path == "/warmup" and DEFAULT_BOT:
            t0 = time.monotonic()
            try:
                snap = self.client.thread(DEFAULT_BOT)
                n = len(extract_bot_replies(snap))
                self._json(
                    200,
                    {
                        "ok": True,
                        "service": "gbot-http",
                        "warmup": "thread",
                        "entries": n,
                        "elapsed_s": time.monotonic() - t0,
                    },
                )
            except Exception as exc:
                self._json(502, {"ok": False, "error": str(exc), "warmup": "thread"})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in {"/send", "/ask"}:
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(data, dict):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        text = str(data.get("text") or "").strip()
        bot = str(
            data.get("target") or data.get("bot") or data.get("target_id") or DEFAULT_BOT
        ).strip() or DEFAULT_BOT
        if not text:
            self._json(400, {"ok": False, "error": "empty text"})
            return
        if not bot:
            self._json(
                400,
                {"ok": False, "error": "no target bot: pass \"target\" or set STACKCHAN_TOOL_BOT"},
            )
            return
        stream = (
            str(self.headers.get("X-Stackchan-Stream") or "").strip() in {"1", "true", "yes"}
            or "stream=1" in self.path
        )
        self._stream_started = False
        t0 = time.monotonic()
        if resolve_gbot_bin() is None:
            payload = {"ok": False, "error": gbot_missing_error(), "first_reply_s": 0.0}
            if stream:
                try:
                    self._begin_ndjson()
                    self._ndjson(payload)
                except Exception:
                    self._json(502, payload)
            else:
                self._json(502, payload)
            return
        try:
            with _lock_for_bot(bot):
                self._handle_send(text, bot, stream=stream, t0=time.monotonic())
        except Exception as exc:
            elapsed = time.monotonic() - t0
            payload = {"ok": False, "error": str(exc), "first_reply_s": elapsed}
            if stream and self._stream_started:
                try:
                    self._ndjson(payload)
                except Exception:
                    pass
            else:
                self._json(502, payload)

    def _finish(self, payload: dict, *, stream: bool, code: int = 200) -> None:
        if stream:
            self._ndjson(payload)
        else:
            self._json(code, payload)

    def _handle_send(self, text: str, bot: str, *, stream: bool, t0: float) -> None:
        snapshot = self.client.thread(bot)
        meta0 = extract_bot_replies_meta(snapshot)
        seen = {eid for eid, _body, _ts in meta0}
        max_seen_ts = max((ts for _e, _b, ts in meta0 if ts), default=0)
        send_started_ms = int(time.time() * 1000)
        cutoff_ts = max(max_seen_ts, send_started_ms - 2000)
        send_state: dict = {}

        def _do_send() -> None:
            try:
                sent, stdout = _run_gbot_send(text, bot)
                send_state["sent"] = sent
                send_state["stdout"] = stdout
                if not sent:
                    try:
                        self.client.send(bot, text)
                        send_state["sent"] = True
                        send_state["via"] = "client.send"
                    except Exception as exc:
                        send_state["error"] = str(exc)
            except Exception as exc:
                send_state["error"] = str(exc)
            finally:
                send_state["done"] = True

        send_thread = threading.Thread(target=_do_send, name="gbot-send", daemon=True)
        send_thread.start()
        try:
            if stream:
                self._begin_ndjson()

            first_deadline = t0 + FIRST_TIMEOUT_S
            hard_deadline = t0 + FOLLOW_HARD_S
            last_change: dict[str, float] = {}
            last_body: dict[str, str] = {}
            emitted: dict[str, str] = {}
            first_sent = ""
            first_s = None
            more_count = 0
            busy = False
            last_emit_at = t0
            last_piece = ""

            def _emit(payload: dict, *, code: int = 200) -> None:
                self._finish(payload, stream=stream, code=code)

            while time.monotonic() < hard_deadline:
                now = time.monotonic()
                items: list[tuple[str, str]] = []
                stdout = send_state.get("stdout") or ""
                immediate = _extract_stdout_reply(stdout) if stdout else ""
                if immediate and first_s is None:
                    items.append(("stdout", immediate))
                try:
                    for eid, body, ts in extract_bot_replies_meta(self.client.thread(bot)):
                        if not body:
                            continue
                        if eid in seen:
                            continue
                        if ts and ts < cutoff_ts:
                            continue
                        items.append((eid, body))
                except Exception:
                    pass

                progressed = False
                for eid, body in items:
                    prev = last_body.get(eid)
                    if prev != body:
                        last_body[eid] = body
                        last_change[eid] = now
                    piece = ready_utterance(body, unchanged_s=now - last_change.get(eid, now))
                    if not piece:
                        continue
                    already = emitted.get(eid, "")
                    if first_s is None:
                        first_s = now - t0
                        first_sent = piece
                        last_piece = piece
                        emitted[eid] = piece
                        last_emit_at = now
                        busy = looks_busy(piece)
                        payload = {
                            "ok": True,
                            "reply": piece,
                            "bot": bot,
                            "first_reply_s": first_s,
                            "via": "gbot-send",
                            "first_sentence": True,
                            "event": "first",
                            "entry_id": eid,
                        }
                        if stream:
                            self._ndjson(payload)
                            progressed = True
                        else:
                            self._json(200, payload)
                            return
                        continue
                    if not stream:
                        continue
                    # Later messages go out whole: wait until the text has not
                    # changed for STABLE_S, then send the part not sent yet.
                    # Sending only the first sentence of each new message and
                    # marking the whole message as sent dropped the bot's
                    # actual conclusion, which is often the second sentence.
                    if now - last_change.get(eid, now) < STABLE_S:
                        continue
                    if already and body.startswith(already):
                        rest = body[len(already):].strip()
                    elif already:
                        rest = body if body != already else ""
                    else:
                        rest = body
                    if not rest:
                        continue
                    if eid not in emitted:
                        more_count += 1
                    emitted[eid] = body
                    last_piece = rest
                    last_emit_at = now
                    if looks_busy(rest):
                        busy = True
                    self._ndjson(
                        {
                            "ok": True,
                            "reply": rest,
                            "bot": bot,
                            "event": "more",
                            "first_reply_s": first_s,
                            "entry_id": eid,
                        }
                    )
                    progressed = True
                    if more_count >= FOLLOW_MAX:
                        self._ndjson(
                            {
                                "ok": True,
                                "reply": last_piece,
                                "bot": bot,
                                "event": "done",
                                "first_reply_s": first_s,
                                "reason": "max",
                            }
                        )
                        return

                if send_state.get("done") and send_state.get("error") and not first_sent:
                    elapsed = time.monotonic() - t0
                    _emit(
                        {"ok": False, "error": send_state["error"], "first_reply_s": elapsed},
                        code=502,
                    )
                    return

                if not first_sent:
                    if now >= first_deadline:
                        break
                    time.sleep(POLL_S)
                    continue

                idle_limit = FOLLOW_BUSY_IDLE_S if busy else FOLLOW_IDLE_S
                if now - last_emit_at >= idle_limit:
                    if stream:
                        self._ndjson(
                            {
                                "ok": True,
                                "reply": last_piece or first_sent,
                                "bot": bot,
                                "event": "done",
                                "first_reply_s": first_s,
                                "reason": "idle",
                            }
                        )
                    return

                time.sleep(POLL_S)
                _ = progressed

            elapsed = time.monotonic() - t0
            if first_sent and stream:
                self._ndjson(
                    {
                        "ok": True,
                        "reply": last_piece or first_sent,
                        "bot": bot,
                        "event": "done",
                        "first_reply_s": first_s,
                        "reason": "timeout",
                    }
                )
                return
            if first_sent:
                return
            payload = {"ok": False, "error": "gbot timeout", "first_reply_s": elapsed}
            if stream:
                self._ndjson(payload)
            else:
                self._json(504, payload)
        finally:
            # The send has its own timeouts (one direct send plus two client retries), so no shorter join limit is added here.
            send_thread.join()


def main() -> int:
    gbot = resolve_gbot_bin()
    try:
        if gbot and DEFAULT_BOT:
            t0 = time.monotonic()
            client = GrokBotClient(gbot)
            snap = client.thread(DEFAULT_BOT)
            Handler._client = client
            print(
                f"gbot-http warmup thread ok entries={len(extract_bot_replies(snap))} "
                f"s={time.monotonic() - t0:.3f}",
                flush=True,
            )
        elif not gbot:
            print(f"gbot-http warmup skipped: {gbot_missing_error()}", flush=True)
        else:
            print("gbot-http warmup skipped: STACKCHAN_TOOL_BOT is not set", flush=True)
    except Exception as exc:
        print(f"gbot-http warmup skipped: {exc}", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"gbot-http listening on http://{HOST}:{PORT} gbot={gbot or 'not-found'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
