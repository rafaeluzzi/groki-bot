# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Hardware-free checks for signed-in Bot audio and cancellation boundaries."""

import asyncio
import base64
import struct
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp.gbot_voice_bridge import (
    GbotVoiceBridge,
    Pcm16To24,
    validate_bridge_url,
    validate_voice_url,
)
from stackchan_mcp.gbot_voice_proxy import GbotVoiceProxy


def bridge():
    return GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "http://evil.test",
        "http://127.0.0.1@evil.test",
        "http://localhost",
        "http://127.0.0.1/?token=x",
    ],
)
def test_bridge_must_be_loopback(url):
    with pytest.raises(ValueError):
        validate_bridge_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ws://api.x.ai",
        "wss://evil.test",
        "wss://api.x.ai.evil.test",
        "wss://secret@api.x.ai",
        "wss://api.x.ai:8443",
    ],
)
def test_temporary_secret_only_goes_to_xai(url):
    with pytest.raises(ValueError):
        validate_voice_url(url)


def test_streaming_resampling_matches_single_chunk():
    pcm = struct.pack("<1600h", *range(1600))
    whole = Pcm16To24().convert(pcm)
    resampler = Pcm16To24()
    chunks = b"".join(
        resampler.convert(pcm[i : i + 320]) for i in range(0, len(pcm), 320)
    )
    assert whole == chunks
    assert abs(len(whole) - len(pcm) * 1.5) <= 4


@pytest.mark.asyncio
async def test_interrupt_discards_late_old_audio_and_accepts_next_turn():
    b = bridge()
    b._send = AsyncMock()
    await b._handle_event({"type": "response.created", "response": {"id": "old"}})
    delta = {
        "type": "response.audio.delta",
        "response_id": "old",
        "delta": base64.b64encode(b"\x00\x00" * 240).decode(),
    }
    await b._handle_event(delta)
    assert b._on_audio.await_count == 1
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    b._on_interrupt.assert_awaited_once()
    await b._handle_event(delta)
    await b._handle_event({"type": "response.done", "response": {"id": "old"}})
    assert b._on_audio.await_count == 1
    assert b._turn_task is None
    await b._handle_event({"type": "response.created", "response": {"id": "next"}})
    await b._handle_event(delta)  # old response arriving even after next starts
    assert b._on_audio.await_count == 1
    await b._handle_event({**delta, "response_id": "next"})
    assert b._on_audio.await_count == 2


@pytest.mark.asyncio
async def test_abort_with_no_active_response_drops_the_in_flight_reply():
    b = bridge()
    b._send = AsyncMock()
    b._quiet.hold_turn(0)
    b._quiet_outstanding = True
    b._quiet_id = "quiet_1"
    await b.interrupt()
    assert b._response_in_progress is False
    assert b._quiet.turn_pending is False
    assert b._quiet_outstanding is False
    assert b._quiet_id is None
    assert b._suppress_next_response is True
    b._send.assert_not_awaited()
    delta = {
        "type": "response.audio.delta",
        "delta": base64.b64encode(b"\x00\x00" * 240).decode(),
    }
    await b._handle_event({"type": "response.created", "response": {"id": "late"}})
    await b._handle_event({**delta, "response_id": "late"})
    assert b._on_audio.await_count == 0
    await b._handle_event({"type": "response.done", "response": {"id": "late"}})
    await b._handle_event({"type": "response.created", "response": {"id": "ask"}})
    await b._handle_event({**delta, "response_id": "ask"})
    assert b._on_audio.await_count == 1


def _pcm_delta(response_id: str) -> dict:
    return {
        "type": "response.audio.delta",
        "response_id": response_id,
        "delta": base64.b64encode(b"\x00\x00" * 240).decode(),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["input_audio_buffer.speech_started", "input_audio_buffer.committed"],
)
async def test_abort_then_the_next_user_turn_plays(kind):
    b = bridge()
    b._send = AsyncMock()
    await b.interrupt()
    assert b._suppress_next_response is True
    await b._handle_event({"type": kind})
    assert b._suppress_next_response is False
    await b._handle_event({"type": "response.created", "response": {"id": "ask"}})
    await b._handle_event(_pcm_delta("ask"))
    assert b._on_audio.await_count == 1


@pytest.mark.asyncio
async def test_abort_then_uplink_audio_lets_the_next_ask_play():
    b = bridge()
    b._send = AsyncMock()
    b._send_pcm = AsyncMock()
    await b.interrupt()
    await b.send_keepalive_audio(b"\x00\x00")
    assert b._suppress_next_response is True
    await b.send_audio(b"\x00\x00")
    assert b._suppress_next_response is False
    await b._handle_event({"type": "response.created", "response": {"id": "ask"}})
    await b._handle_event(_pcm_delta("ask"))
    assert b._on_audio.await_count == 1


@pytest.mark.asyncio
async def test_late_reply_before_the_next_turn_stays_dropped():
    b = bridge()
    b._send = AsyncMock()
    await b.interrupt()
    await b._handle_event({"type": "response.created", "response": {"id": "late"}})
    await b._handle_event(_pcm_delta("late"))
    assert b._on_audio.await_count == 0


@pytest.mark.asyncio
async def test_abort_with_no_tts_still_sends_a_stop_ack():
    class Codec:
        def reset_encode_buffer(self):
            pass

    class Bridge:
        running = True

        def __init__(self):
            self._connected_event = asyncio.Event()
            self._connected_event.set()
            self.calls = 0

        async def interrupt(self):
            self.calls += 1

    proxy = GbotVoiceProxy(codec_factory=Codec)
    proxy._bridge = Bridge()
    proxy._codec = Codec()
    proxy._send_to_device = AsyncMock()
    proxy._ready_event.set()
    proxy._tts_active = False
    assert await proxy.send_device_json({"type": "abort"}) is True
    assert proxy._bridge.calls == 1
    proxy._send_to_device.assert_awaited_once_with('{"type": "tts", "state": "stop"}')


@pytest.mark.asyncio
async def test_speech_started_cancels_drain_without_blocking_receiver():
    b = bridge()
    b._send = AsyncMock()
    started = asyncio.Event()

    async def drain():
        started.set()
        await asyncio.Event().wait()

    b._on_turn_complete = drain
    await b._handle_event({"type": "response.created", "response": {"id": "old"}})
    await b._handle_event({"type": "response.done", "response": {"id": "old"}})
    await started.wait()
    task = b._turn_task
    await asyncio.wait_for(
        b._handle_event({"type": "input_audio_buffer.speech_started"}), 0.2
    )
    assert task.cancelled()
    b._on_interrupt.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_never_executes_harness_tools(monkeypatch):
    monkeypatch.delenv("STACKCHAN_GBOT_VOICE_TOOLS", raising=False)
    b = bridge()
    b._tool = AsyncMock()
    await b._handle_event(
        {"type": "response.function_call_arguments.done", "name": "send"}
    )
    assert not b._tools
    b._tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_start_does_not_require_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    fake = bridge()
    fake.start = AsyncMock()
    fake.wait_connected = AsyncMock(return_value=True)
    fake.running = True
    fake._connected_event.set()
    proxy = GbotVoiceProxy(bridge_factory=lambda **kwargs: fake)
    assert await proxy.start(type("Info", (), {"device_id": "test"})(), {}, AsyncMock())
    assert proxy.connected
    assert proxy.server_hello["provider"] == "gbot_voice"
    await proxy.stop()


@pytest.mark.asyncio
async def test_interrupt_clears_proxy_pcm_queue_and_sends_stop():
    class Codec:
        def reset_encode_buffer(self):
            pass

    proxy = GbotVoiceProxy(codec_factory=Codec)
    proxy._codec = Codec()
    proxy._send_to_device = AsyncMock()
    proxy._tts_active = True
    proxy._pcm_prebuffer.extend(b"stale-pcm")
    proxy._frame_queue.put_nowait(b"stale-opus")
    proxy._signal_speaking_leds = lambda *args: None
    proxy._drain_task = asyncio.create_task(asyncio.Event().wait())
    await proxy._abort_in_flight_tts()
    assert proxy._frame_queue.empty()
    assert not proxy._pcm_prebuffer
    assert not proxy._tts_active
    assert proxy._drain_task is None
    proxy._send_to_device.assert_awaited_once_with('{"type": "tts", "state": "stop"}')


@pytest.mark.asyncio
async def test_startup_error_redacts_upstream_secrets(caplog):
    b = bridge()
    b._local = AsyncMock(side_effect=RuntimeError("SECRET_FIXTURE"))
    with pytest.raises(RuntimeError, match="details redacted") as caught:
        await b.start()
    assert "SECRET_FIXTURE" not in str(caught.value)
    assert "SECRET_FIXTURE" not in caplog.text
    assert "stage=account_requests error_type=RuntimeError" in caplog.text
    assert b.metrics["startup_stage"] == "account_requests"
    assert b.metrics["startup_error_type"] == "RuntimeError"
    assert b._http.closed


def test_model_is_bound_to_signed_in_credential():
    from stackchan_mcp.gbot_voice_bridge import voice_url_with_model

    assert voice_url_with_model(
        "wss://api.x.ai/v1/realtime?model=old", "signed/model"
    ) == ("wss://api.x.ai/v1/realtime?model=signed%2Fmodel")


@pytest.mark.asyncio
async def test_first_speech_start_does_not_cancel_or_count_interruption():
    b = bridge()
    b._send = AsyncMock()
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    assert b.metrics["speech_starts"] == 1
    assert b.metrics["interruptions"] == 0
    b._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_metrics_only_store_allowlisted_enums():
    b = bridge()
    await b._handle_event(
        {
            "type": "error",
            "error": {
                "code": "SECRET_CODE",
                "param": "SECRET_PARAM",
                "message": "SECRET_BODY",
            },
        }
    )
    assert b.metrics["server_errors"] == {"other:other": 1}


@pytest.mark.asyncio
async def test_late_old_done_does_not_finish_new_response():
    b = bridge()
    b._send = AsyncMock()
    await b._handle_event({"type": "response.created", "response": {"id": "old"}})
    await b.interrupt()
    await b._handle_event({"type": "response.created", "response": {"id": "next"}})
    await b._handle_event({"type": "response.done", "response": {"id": "old"}})
    assert b._response_in_progress
    assert b._active_response == "next"
    assert b._turn_task is None


@pytest.mark.asyncio
async def test_native_registration_failure_keeps_voice_start_running(monkeypatch):
    import time
    from types import SimpleNamespace
    from stackchan_mcp import gbot_voice_bridge as module

    b = bridge()
    ws = SimpleNamespace(closed=False, send_json=AsyncMock(), close=AsyncMock())
    http = SimpleNamespace(ws_connect=AsyncMock(return_value=ws), close=AsyncMock())
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda **kwargs: http)

    async def local(path, payload=None):
        if path == "/credential":
            return {
                "credential": {
                    "websocketUrl": "wss://api.x.ai/v1/realtime",
                    "model": "fixture",
                    "clientSecret": "PRIVATE_FIXTURE",
                    "expiresAtUnixSeconds": time.time() + 60,
                }
            }
        return {"instructions": "fixture", "tools": []}

    b._local = local

    def configure(session):
        b._desktop_enabled = True
        return []

    b._configure_tools = configure

    async def receive():
        b._connected_event.set()
        await asyncio.Event().wait()

    b._receive = receive
    monkeypatch.setattr(
        module.NativeVoiceChannel,
        "_presence",
        AsyncMock(side_effect=RuntimeError("offline")),
    )
    await asyncio.wait_for(b.start(), 0.2)
    assert await b.wait_connected(0.2)
    assert b.running and not b._native.ready
    await b.stop()
    assert b._native._task.done()
