# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""The authenticated helper boundary, using client-native update envelopes."""

import asyncio
from unittest.mock import AsyncMock

from aiohttp import web
import pytest

from stackchan_mcp.gbot_native_channel import NativeVoiceChannel, update_event


def test_work_landed_is_native_data_and_cannot_grant_new_user_authority():
    event, is_user = update_event(
        {
            "channel": "voice-call-nudge",
            "payload": {
                "agentId": "fixture-agent",
                "callId": "fixture-call",
                "kind": "outcome",
                "update": "Finished fixture task.",
            },
        },
        "fixture-agent",
        "fixture-call",
    )
    assert event["type"] == "async_update.add"
    assert event["async_update"]["topic"] == "work_landed"
    assert event["async_update"]["payload"][0]["message"] == {
        "type": "text",
        "content": "Finished fixture task.",
    }
    assert not is_user


def test_only_filtered_current_user_message_becomes_a_user_update():
    message = {
        "channel": "typed-message",
        "payload": {
            "agentId": "fixture-agent",
            "callId": "fixture-call",
            "entryId": "entry-7",
            "text": "Say hello.",
            "atMs": 123456789,
        },
    }
    event, is_user = update_event(message, "fixture-agent", "fixture-call")
    assert is_user
    assert event["async_update"]["topic"] == "user_message"
    assert event["event_id"] == "written-turn-entry-7"
    assert event["async_update"]["payload"][0]["timestampMs"] == 123456789
    assert update_event(message, "another-agent", "fixture-call") is None
    assert update_event(message, "fixture-agent", "another-call") is None


@pytest.mark.asyncio
async def test_native_channel_deduplicates_typed_and_drops_after_close():
    callback = AsyncMock()
    channel = NativeVoiceChannel(
        None, "http://127.0.0.1:18771", "agent", "call", callback
    )
    message = {
        "channel": "typed-message",
        "payload": {
            "agentId": "agent",
            "callId": "call",
            "entryId": "one",
            "text": "fixture",
            "atMs": 10,
        },
    }
    await channel.accept_event(message)
    await channel.accept_event(message)
    assert callback.await_count == 1
    await channel.close()
    await channel.accept_event(
        {**message, "payload": {**message["payload"], "entryId": "two"}}
    )
    assert callback.await_count == 1


@pytest.mark.asyncio
async def test_native_presence_and_sse_lifecycle():
    from aiohttp import ClientSession, web
    import json

    presence = []
    release = asyncio.Event()

    async def register(request):
        presence.append(await request.json())
        return web.json_response({"ok": True})

    async def events(request):
        assert request.query == {"agentId": "agent", "callId": "call"}
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        payload = {
            "channel": "voice-call-nudge",
            "payload": {
                "agentId": "agent",
                "callId": "call",
                "kind": "outcome",
                "update": "fixture result",
            },
        }
        await stream.write(("data: " + json.dumps(payload) + "\n\n").encode())
        await release.wait()
        return stream

    app = web.Application()
    app.router.add_post("/native/presence", register)
    app.router.add_get("/native/events", events)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    got = asyncio.Event()
    received = []

    async def callback(event, is_user):
        received.append((event, is_user))
        got.set()

    try:
        async with ClientSession() as http:
            channel = NativeVoiceChannel(
                http, f"http://127.0.0.1:{port}", "agent", "call", callback
            )
            await channel.start()
            await asyncio.wait_for(got.wait(), 1)
            assert channel.connected.is_set()
            assert channel.health() == {
                "state": "ready",
                "ready": True,
                "presence_status": 200,
                "error": None,
            }
            await channel.close()
        assert [item["isOnTheLine"] for item in presence] == [True, False]
        assert all(item["acceptsOverheard"] is False for item in presence)
        assert received[0][0]["async_update"]["topic"] == "work_landed"
        assert received[0][1] is False
        assert not channel.connected.is_set()
        closed = channel.health()
        assert closed["state"] == "unavailable" and closed["ready"] is False
        assert closed["presence_status"] == 200
    finally:
        release.set()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_unavailable_presence_does_not_block_channel_start_and_stop_cancels():
    channel = NativeVoiceChannel(
        None, "http://127.0.0.1:18771", "agent", "call", AsyncMock()
    )
    channel._presence = AsyncMock(side_effect=RuntimeError("offline"))
    await asyncio.wait_for(channel.start(), 0.1)
    await asyncio.sleep(0)
    assert not channel.ready
    await channel.close()
    assert channel._task.done()


@pytest.mark.asyncio
async def test_presence_retry_recovers_readiness_without_restarting_voice():
    channel = NativeVoiceChannel(
        None, "http://127.0.0.1:18771", "agent", "call", AsyncMock()
    )
    channel._retry_delay = 0.001
    calls = 0

    async def presence(on):
        nonlocal calls
        if on:
            calls += 1
            if calls == 1:
                raise RuntimeError("offline")

    async def listen():
        channel.connected.set()
        await asyncio.Event().wait()

    channel._presence = presence
    channel._listen = listen
    await channel.start()
    await asyncio.wait_for(channel.connected.wait(), 0.2)
    assert channel.ready and calls == 2
    await channel.close()
    assert not channel.ready and channel._task.done()


@pytest.mark.asyncio
async def test_stop_cancels_inflight_registration_before_any_late_ready():
    channel = NativeVoiceChannel(
        None, "http://127.0.0.1:18771", "agent", "call", AsyncMock()
    )
    entered = asyncio.Event()

    async def presence(on):
        if on:
            entered.set()
            await asyncio.Event().wait()

    channel._presence = presence
    await channel.start()
    await entered.wait()
    await asyncio.wait_for(channel.close(), 0.1)
    assert channel._task.cancelled()
    assert not channel.ready


async def eventually(predicate, timeout=2):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


class _NativeHelper:
    def __init__(self):
        self.on_status = 502
        self.events_status = 502
        self.release = asyncio.Event()
        self.event_calls = 0

    async def register(self, request):
        body = await request.json()
        if not body.get("isOnTheLine"):
            return web.json_response({"ok": True})
        status = self.on_status
        if status != 200:
            # Same body the real helper sends when Grok's sandbox answers 429.
            return web.json_response({"ok": False, "error": "upstream_http_429"}, status=status)
        return web.json_response({"ok": True}, status=status)

    async def events(self, request):
        self.event_calls += 1
        if self.events_status != 200:
            return web.Response(status=self.events_status)
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        await stream.write(b": connected\n\n")
        await self.release.wait()
        return stream


@pytest.mark.asyncio
async def test_voice_health_reports_native_502_and_recovers_without_dropping_voice():
    """Presence 502 and a dead event stream are native_not_ready; voice stays up."""
    from aiohttp import ClientSession
    from types import SimpleNamespace

    from stackchan_mcp.capture_server import create_capture_app
    from stackchan_mcp.debug_status import DebugStatus
    from stackchan_mcp.gbot_voice_proxy import GbotVoiceProxy

    helper = _NativeHelper()
    status = DebugStatus()
    app = create_capture_app(debug_status=status)
    app.router.add_post("/native/presence", helper.register)
    app.router.add_get("/native/events", helper.events)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    proxy = GbotVoiceProxy(debug_status=status, wake_gate_factory=lambda: None)
    proxy._stopped = False
    proxy._codec = object()
    proxy._send_to_device = object()
    proxy._ready_event.set()
    proxy._health["state"] = "connected"
    status.voice_health_provider = proxy.health_snapshot

    async def unused_update(event, is_user):
        return None

    try:
        async with ClientSession() as http:
            channel = NativeVoiceChannel(http, base, "agent", "call", unused_update)
            channel._retry_delay = 0.01
            bridge = SimpleNamespace(
                running=True, _connected_event=asyncio.Event(), _native=channel
            )
            bridge._connected_event.set()
            proxy._bridge = bridge
            try:
                await channel.start()

                async def read_health():
                    async with http.get(base + "/debug/voice-health") as response:
                        return response.status, await response.json()

                await eventually(lambda: channel.health()["presence_status"] == 502)
                assert channel.health() == {
                    "state": "unavailable",
                    "ready": False,
                    "presence_status": 502,
                    "error": "upstream_http_429",
                }
                assert not channel.ready
                http_status, body = await read_health()
                assert http_status == 200 and body["ok"] is True
                assert body["state"] == "connected"
                assert body["native"] == channel.health()

                helper.on_status = 200
                seen_events = helper.event_calls
                await eventually(
                    lambda: helper.event_calls > seen_events
                    and channel.health()
                    == {
                        "state": "unavailable",
                        "ready": False,
                        "presence_status": 200,
                        "error": "http_502",
                    }
                )
                http_status, body = await read_health()
                assert http_status == 200 and body["ok"] is True
                assert body["native"]["ready"] is False
                assert body["native"]["presence_status"] == 200

                helper.events_status = 200
                await eventually(lambda: channel.ready)
                assert channel.health() == {
                    "state": "ready",
                    "ready": True,
                    "presence_status": 200,
                    "error": None,
                }
                http_status, body = await read_health()
                assert http_status == 200 and body["ok"] is True
                assert body["state"] == "connected"
                assert body["native"] == {
                    "state": "ready",
                    "ready": True,
                    "presence_status": 200,
                    "error": None,
                }
            finally:
                helper.release.set()
                await channel.close()
    finally:
        await runner.cleanup()
