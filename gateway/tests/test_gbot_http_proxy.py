# SPDX-FileCopyrightText: 2026 sefuzhou770801-hub
# SPDX-License-Identifier: MIT
"""Grok Bot forwarding service (gbot_http_proxy): HTTP tests against a fake gbot.

The service runs as a real subprocess (``python -m stackchan_mcp.gbot_http_proxy``),
the same entry point as ``uv run stackchan-gbot-proxy``.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATEWAY_DIR = HERE.parent
FAKE_GBOT = HERE / "_fake_gbot.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ProxyServer:
    def __init__(self, tmp: Path, extra_env: dict[str, str] | None = None) -> None:
        self.port = _free_port()
        self.state = tmp / "fake-gbot-state.json"
        self.state.write_text('{"entries": [], "seq": 0}', encoding="utf-8")
        wrapper = tmp / "gbot"
        wrapper.write_text(
            "#!/bin/sh\n"
            f'exec {sys.executable} "{FAKE_GBOT}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "STACKCHAN_GBOT_BIN": str(wrapper),
                "STACKCHAN_GBOT_HTTP_PORT": str(self.port),
                "STACKCHAN_ASK_BOT": "助手",
                "STACKCHAN_GBOT_POLL_S": "0.05",
                "STACKCHAN_GBOT_STABLE_S": "0.05",
                "STACKCHAN_GBOT_FIRST_TIMEOUT": "5",
                "STACKCHAN_GBOT_FOLLOW_S": "5",
                "FAKE_GBOT_STATE": str(self.state),
                "PYTHONUNBUFFERED": "1",
                "PYTHONPATH": str(GATEWAY_DIR),
                "STACKCHAN_SKIP_DOTENV": "1",
            }
        )
        env.pop("STACKCHAN_TOOL_BOT", None)
        if extra_env:
            env.update(extra_env)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "stackchan_mcp.gbot_http_proxy"],
            env=env,
            cwd=str(tmp),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def wait_ready(self, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                stdout, stderr = self.proc.communicate(timeout=1)
                raise RuntimeError(
                    "proxy exited early "
                    f"code={self.proc.returncode} stdout={stdout.decode()} stderr={stderr.decode()}"
                )
            try:
                self.request("GET", "/health")
                return
            except Exception as exc:  # noqa: BLE001 — connection refused is expected during start-up
                last_error = exc
                time.sleep(0.05)
        raise TimeoutError(f"proxy did not become ready: {last_error}")

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 8,
    ) -> tuple[int, dict]:
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method=method,
            headers=req_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return resp.status, payload
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return exc.code, payload
            finally:
                exc.close()

    def stream(self, body: bytes, timeout: float = 10) -> list[dict]:
        """POST /send with X-Stackchan-Stream and return every NDJSON event."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/send",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "X-Stackchan-Stream": "1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            assert "ndjson" in (resp.headers.get("Content-Type") or "")
            return [json.loads(line) for line in resp.read().decode("utf-8").splitlines() if line]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)


class SendRejectsNonObjectJson(unittest.TestCase):
    def _assert_bad_json(self, body: bytes) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = ProxyServer(Path(tmp))
            try:
                server.wait_ready()
                code, payload = server.request("POST", "/send", body=body)
                self.assertEqual(code, 400)
                self.assertEqual(payload.get("ok"), False)
                self.assertEqual(payload.get("error"), "bad json")
            finally:
                server.close()

    def test_number_body_returns_400_json(self) -> None:
        self._assert_bad_json(b"42")

    def test_string_body_returns_400_json(self) -> None:
        self._assert_bad_json(b'"text"')

    def test_array_body_returns_400_json(self) -> None:
        self._assert_bad_json(b"[1]")


class StreamFailureSendsHttpStatus(unittest.TestCase):
    def test_thread_auth_failure_returns_502_before_ndjson(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = ProxyServer(Path(tmp), extra_env={"FAKE_GBOT_THREAD_FAIL": "1"})
            try:
                server.wait_ready()
                try:
                    code, payload = server.request(
                        "POST",
                        "/send",
                        body=b'{"text":"ping"}',
                        headers={"X-Stackchan-Stream": "1"},
                    )
                except (http.client.BadStatusLine, http.client.RemoteDisconnected) as exc:
                    self.fail(f"streaming failure wrote a body without HTTP headers: {exc}")
                self.assertEqual(code, 502)
                self.assertEqual(payload.get("ok"), False)
                self.assertTrue(str(payload.get("error") or ""))
            finally:
                server.close()


class ConcurrentSameBotSend(unittest.TestCase):
    def test_overlapping_sends_return_matching_replies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = ProxyServer(
                Path(tmp),
                extra_env={"FAKE_GBOT_THREAD_DELAY_S": "0.30"},
            )
            try:
                server.wait_ready()
                results: dict[str, tuple[int, dict]] = {}
                errors: dict[str, Exception] = {}

                def post(name: str) -> None:
                    try:
                        results[name] = server.request(
                            "POST",
                            "/send",
                            body=json.dumps({"text": name}, ensure_ascii=False).encode("utf-8"),
                        )
                    except Exception as exc:  # noqa: BLE001 — collect the thread's exception for the main-thread assert
                        errors[name] = exc

                workers = [
                    threading.Thread(target=post, args=("alpha",)),
                    threading.Thread(target=post, args=("beta",)),
                ]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=10)
                    self.assertFalse(worker.is_alive(), "request thread did not finish")

                self.assertEqual(errors, {})
                code_a, payload_a = results["alpha"]
                code_b, payload_b = results["beta"]
                self.assertEqual(code_a, 200)
                self.assertEqual(code_b, 200)
                self.assertEqual(payload_a.get("ok"), True)
                self.assertEqual(payload_b.get("ok"), True)
                self.assertEqual(payload_a.get("reply"), "reply-alpha。")
                self.assertEqual(payload_b.get("reply"), "reply-beta。")
            finally:
                server.close()

    def test_late_send_after_timeout_does_not_feed_next_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = ProxyServer(
                Path(tmp),
                extra_env={
                    "STACKCHAN_GBOT_FIRST_TIMEOUT": "0.40",
                    "FAKE_GBOT_SEND_DELAY_S": "0.70",
                },
            )
            try:
                server.wait_ready()
                code_a, payload_a = server.request("POST", "/send", body=b'{"text":"alpha"}')
                self.assertEqual(code_a, 504)
                self.assertEqual(payload_a.get("ok"), False)
                self.assertEqual(payload_a.get("error"), "gbot timeout")

                code_b, payload_b = server.request("POST", "/send", body=b'{"text":"beta"}')
                self.assertEqual(code_b, 200)
                self.assertEqual(payload_b.get("ok"), True)
                self.assertEqual(payload_b.get("reply"), "reply-beta。")
            finally:
                server.close()

    def test_send_past_scaled_old_join_cap_does_not_leak(self) -> None:
        # By default a send takes up to about 60 s and the old join limit was 25 s. With the send timeout cut from 20 to 4,
        # the 25 s limit scales to 5 s. A 3.2 s delay is below one send timeout (the process can finish its reply)
        # but well above the first-sentence timeout, so send still runs about 2.8 s after the 504; join must wait for the thread.
        send_timeout = 4.0
        delay = 3.2
        with tempfile.TemporaryDirectory() as tmp:
            server = ProxyServer(
                Path(tmp),
                extra_env={
                    "STACKCHAN_GBOT_SEND_TIMEOUT_S": str(send_timeout),
                    "STACKCHAN_GBOT_FIRST_TIMEOUT": "0.40",
                    "FAKE_GBOT_SEND_DELAY_S": str(delay),
                },
            )
            try:
                server.wait_ready()
                code_a, payload_a = server.request(
                    "POST",
                    "/send",
                    body=b'{"text":"alpha"}',
                    timeout=15,
                )
                self.assertEqual(code_a, 504)
                self.assertEqual(payload_a.get("ok"), False)
                self.assertEqual(payload_a.get("error"), "gbot timeout")

                code_b, payload_b = server.request(
                    "POST",
                    "/send",
                    body=b'{"text":"beta"}',
                    timeout=15,
                )
                self.assertEqual(code_b, 200)
                self.assertEqual(payload_b.get("ok"), True)
                self.assertEqual(payload_b.get("reply"), "reply-beta。")
            finally:
                server.close()


class HealthAndSendHappyPath(unittest.TestCase):
    def test_health_reports_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = ProxyServer(Path(tmp))
            try:
                server.wait_ready()
                code, payload = server.request("GET", "/health")
                self.assertEqual(code, 200)
                self.assertEqual(payload.get("ok"), True)
                self.assertEqual(payload.get("service"), "gbot-http")
                self.assertEqual(payload.get("listen"), server.port)
            finally:
                server.close()

    def test_send_returns_fake_gbot_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = ProxyServer(Path(tmp))
            try:
                server.wait_ready()
                code, payload = server.request(
                    "POST",
                    "/send",
                    body=b'{"text":"ping"}',
                )
                self.assertEqual(code, 200)
                self.assertEqual(payload.get("ok"), True)
                self.assertEqual(payload.get("reply"), "reply-ping。")
            finally:
                server.close()


class StreamFollowsLaterMessages(unittest.TestCase):
    def test_later_message_is_sent_whole_not_just_first_sentence(self) -> None:
        """Regression: a later bot message used to be cut to its first sentence,
        which dropped the actual result."""
        with tempfile.TemporaryDirectory() as tmp:
            server = ProxyServer(
                Path(tmp),
                extra_env={
                    "FAKE_GBOT_EXTRA_REPLY": "查到了。结论是明天下雨。",
                    "STACKCHAN_GBOT_FOLLOW_IDLE_S": "0.8",
                    "STACKCHAN_GBOT_FOLLOW_BUSY_IDLE_S": "0.8",
                },
            )
            try:
                server.wait_ready()
                events = server.stream(b'{"text":"weather"}')
                kinds = [event.get("event") for event in events]
                self.assertEqual(kinds[0], "first")
                self.assertEqual(events[0].get("reply"), "reply-weather。")
                more = "".join(e.get("reply", "") for e in events if e.get("event") == "more")
                self.assertIn("查到了。", more)
                self.assertIn("结论是明天下雨。", more)
                self.assertEqual(kinds[-1], "done")
            finally:
                server.close()


class GatewayClientEndToEnd(unittest.TestCase):
    """gbot_brain.iter_gbot_replies (used by ask_grokbot) against the real service."""

    def test_iter_gbot_replies_streams_every_piece(self) -> None:
        from stackchan_mcp import gbot_brain

        with tempfile.TemporaryDirectory() as tmp:
            server = ProxyServer(
                Path(tmp),
                extra_env={
                    "FAKE_GBOT_EXTRA_REPLY": "办好了。网页已经打开。",
                    "STACKCHAN_GBOT_FOLLOW_IDLE_S": "0.8",
                    "STACKCHAN_GBOT_FOLLOW_BUSY_IDLE_S": "0.8",
                },
            )
            try:
                server.wait_ready()
                old = os.environ.get("STACKCHAN_GBOT_URL")
                os.environ["STACKCHAN_GBOT_URL"] = f"http://127.0.0.1:{server.port}"
                try:
                    events = list(
                        gbot_brain.iter_gbot_replies("open", bot="助手", bot_id="", timeout_s=10)
                    )
                finally:
                    if old is None:
                        os.environ.pop("STACKCHAN_GBOT_URL", None)
                    else:
                        os.environ["STACKCHAN_GBOT_URL"] = old
                replies = [event["reply"] for event in events]
                self.assertEqual(replies[0], "reply-open。")
                self.assertIn("办好了。网页已经打开。", "".join(replies[1:]))
                self.assertEqual(events[0]["event"], "first")
            finally:
                server.close()

    def test_iter_gbot_replies_raises_when_service_is_down(self) -> None:
        from stackchan_mcp import gbot_brain

        old = os.environ.get("STACKCHAN_GBOT_URL")
        os.environ["STACKCHAN_GBOT_URL"] = f"http://127.0.0.1:{_free_port()}"
        try:
            with self.assertRaises(gbot_brain.GbotBrainError):
                list(gbot_brain.iter_gbot_replies("ping", bot="助手", timeout_s=2))
        finally:
            if old is None:
                os.environ.pop("STACKCHAN_GBOT_URL", None)
            else:
                os.environ["STACKCHAN_GBOT_URL"] = old


class NoTargetBot(unittest.TestCase):
    def test_send_without_target_and_without_default_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = ProxyServer(Path(tmp), extra_env={"STACKCHAN_ASK_BOT": ""})
            try:
                server.wait_ready()
                code, payload = server.request("POST", "/send", body=b'{"text":"ping"}')
                self.assertEqual(code, 400)
                self.assertIn("STACKCHAN_TOOL_BOT", payload.get("error", ""))
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
