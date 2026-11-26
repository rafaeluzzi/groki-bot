"""Tests for the debug status module and the /debug/status and /debug/panel endpoints."""

import json
from types import SimpleNamespace

import pytest

from stackchan_mcp.capture_server import (
    DEBUG_STATUS_KEY,
    create_capture_app,
    handle_debug_panel,
    handle_debug_status,
)
from stackchan_mcp.debug_status import (
    RECENT_LIMIT,
    DebugStatus,
    get_debug_status,
    reset_debug_status,
)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeAppRequest:
    def __init__(self, app) -> None:
        self.app = app


# --- singleton ---------------------------------------------------------------


def test_singleton_returns_same_instance_until_reset():
    reset_debug_status()
    first = get_debug_status()
    assert get_debug_status() is first
    reset_debug_status()
    assert get_debug_status() is not first


# --- device events ---------------------------------------------------------


def test_device_connect_disconnect_updates_counters():
    clock = FakeClock(10.0)
    st = DebugStatus(clock=clock)

    st.on_device_connected("dev-1")
    snap = st.snapshot()["device"]
    assert snap["connected"] is True
    assert snap["device_id"] == "dev-1"
    assert snap["connected_since"] == 10.0

    clock.now = 20.0
    st.on_device_disconnected()
    snap = st.snapshot()["device"]
    assert snap["connected"] is False
    assert snap["last_disconnect_at"] == 20.0
    assert snap["disconnect_count"] == 1


# --- gemini events ---------------------------------------------------------


def test_gemini_connect_records_session_count_and_since():
    clock = FakeClock(50.0)
    st = DebugStatus(clock=clock)
    st.on_gemini_started()
    st.on_gemini_connected(3)
    snap = st.snapshot()["gemini"]
    assert snap["running"] is True
    assert snap["connected"] is True
    assert snap["session_count"] == 3
    assert snap["connected_since"] == 50.0


def test_gemini_disconnect_records_error_and_1008_counter():
    st = DebugStatus(clock=FakeClock(60.0))
    st.on_gemini_connected(1)

    active = st.on_gemini_disconnected(error="socket closed 1008", code=1008)
    snap = st.snapshot()["gemini"]
    assert active is False  # not LISTENING and no TTS → not an active drop
    assert snap["connected"] is False
    assert snap["reconnect_1008_count"] == 1


def test_resumption_and_receive_stall_fields_in_snapshot():
    st = DebugStatus(clock=lambda: 10.0)
    st.on_gemini_connected(2, used_resumption_handle=True)
    st.on_resumption_handle_updated()
    st.on_gemini_receive_stall_reconnect()

    snap = st.snapshot()["gemini"]
    assert snap["has_resumption_handle"] is True
    assert snap["last_reconnect_used_handle"] is True
    assert snap["resumption_handle_updated_at"] == 10.0
    assert snap["reconnect_receive_stall_count"] == 1


def test_wake_gate_unavailable_sets_explicit_state():
    st = DebugStatus()
    st.on_wake_gate_configured(available=True, state="LISTENING")
    st.on_wake_gate_unavailable()

    snap = st.snapshot()["wake_gate"]
    assert snap["available"] is False
    assert snap["state"] == "UNAVAILABLE"


def test_gemini_disconnect_during_listening_counts_active_drop():
    st = DebugStatus(clock=FakeClock(70.0))
    st.on_gemini_connected(1)
    st.on_wake_woke()

    active = st.on_gemini_disconnected(error="boom", code=None)
    assert active is True
    assert st.snapshot()["gemini"]["active_drops"] == 1
    assert st.snapshot()["gemini"]["last_active_drop_at"] == 70.0


def test_gemini_disconnect_during_tts_counts_active_drop():
    st = DebugStatus()
    st.on_gemini_connected(1)
    st.on_tts_state(True)

    assert st.on_gemini_disconnected(error="boom") is True
    assert st.snapshot()["gemini"]["active_drops"] == 1


def test_gemini_disconnect_without_prior_connection_is_not_active_drop():
    st = DebugStatus()
    st.on_wake_woke()  # LISTENING, but the session never came up

    active = st.on_gemini_disconnected(error="connect failed", was_connected=False)
    assert active is False
    assert st.snapshot()["gemini"]["active_drops"] == 0


def test_gemini_stopped_clears_running_and_connected():
    st = DebugStatus()
    st.on_gemini_started()
    st.on_gemini_connected(1)
    st.on_gemini_stopped()
    snap = st.snapshot()["gemini"]
    assert snap["running"] is False
    assert snap["connected"] is False


def test_stale_generation_lifecycle_does_not_overwrite_current_flags():
    st = DebugStatus()
    old = st.next_gemini_generation()
    st.on_gemini_started(generation=old)
    st.on_gemini_connected(1, generation=old)

    current = st.next_gemini_generation()
    st.on_gemini_started(generation=current)
    st.on_gemini_connected(2, generation=current)
    st.on_wake_woke()

    st.on_gemini_disconnected(error="old instance closed", generation=old)
    st.on_gemini_stopped(generation=old)

    snap = st.snapshot()["gemini"]
    assert snap["generation"] == current
    assert snap["running"] is True
    assert snap["connected"] is True
    assert snap["session_count"] == 2
    assert snap["active_drops"] == 0
    assert snap["last_error"] is None


def test_current_generation_stop_still_clears_running():
    st = DebugStatus()
    generation = st.next_gemini_generation()
    st.on_gemini_started(generation=generation)
    st.on_gemini_connected(1, generation=generation)
    st.on_gemini_stopped(generation=generation)
    snap = st.snapshot()["gemini"]
    assert snap["running"] is False
    assert snap["connected"] is False
    assert snap["generation"] == generation


def test_keepalive_sent_updates_counter_and_timestamp():
    clock = FakeClock(80.0)
    st = DebugStatus(clock=clock)
    st.on_keepalive_sent()
    clock.now = 90.0
    st.on_keepalive_sent()
    snap = st.snapshot()["gemini"]
    assert snap["keepalive_count"] == 2
    assert snap["last_keepalive_at"] == 90.0


def test_tool_call_cancel_updates_counter_and_timestamp():
    clock = FakeClock(95.0)
    st = DebugStatus(clock=clock)
    st.on_tool_call_cancelled()
    clock.now = 96.0
    st.on_tool_call_cancelled()
    snap = st.snapshot()["gemini"]
    assert snap["tool_call_cancel_count"] == 2
    assert snap["last_tool_call_cancel_at"] == 96.0


def test_keepalive_lifecycle_records_running_and_skip_reason():
    clock = FakeClock(100.0)
    st = DebugStatus(clock=clock)

    st.on_keepalive_disabled("wake_gate_absent")
    snap = st.snapshot()["gemini"]
    assert snap["keepalive_running"] is False
    assert snap["keepalive_disabled_reason"] == "wake_gate_absent"

    st.on_keepalive_started()
    clock.now = 110.0
    st.on_keepalive_skipped("listening")
    snap = st.snapshot()["gemini"]
    assert snap["keepalive_running"] is True
    assert snap["keepalive_disabled_reason"] is None
    assert snap["last_keepalive_skip_reason"] == "listening"
    assert snap["last_keepalive_skip_at"] == 110.0

    st.on_keepalive_sent()
    snap = st.snapshot()["gemini"]
    assert snap["last_keepalive_skip_reason"] is None
    assert snap["last_keepalive_skip_at"] is None

    st.on_keepalive_stopped()
    assert st.snapshot()["gemini"]["keepalive_running"] is False


def test_usage_metadata_updates_token_usage_snapshot():
    st = DebugStatus(clock=FakeClock(120.0))

    st.record_usage_metadata(
        SimpleNamespace(
            total_token_count=88,
            prompt_token_count=70,
            response_token_count=12,
            tool_use_prompt_token_count=6,
            prompt_tokens_details=[
                SimpleNamespace(modality=SimpleNamespace(value="TEXT"), token_count=30),
                SimpleNamespace(modality=SimpleNamespace(value="AUDIO"), token_count=40),
            ],
            response_tokens_details=[
                SimpleNamespace(modality=SimpleNamespace(value="AUDIO"), token_count=12)
            ],
        )
    )

    usage = st.snapshot()["gemini"]["token_usage"]
    assert usage["total_token_count"] == 88
    assert usage["prompt_token_count"] == 70
    assert usage["response_token_count"] == 12
    assert usage["tool_use_prompt_token_count"] == 6
    assert usage["prompt_tokens_details"] == [
        {"modality": "TEXT", "token_count": 30},
        {"modality": "AUDIO", "token_count": 40},
    ]
    assert usage["response_tokens_details"] == [
        {"modality": "AUDIO", "token_count": 12}
    ]
    assert usage["updated_at"] == 120.0



# --- wake_gate / audio events -----------------------------------------------


def test_wake_gate_events_update_state_and_counters():
    clock = FakeClock(30.0)
    st = DebugStatus(clock=clock)

    st.on_wake_gate_configured(available=True)
    snap = st.snapshot()["wake_gate"]
    assert snap["available"] is True
    assert snap["state"] == "DORMANT"

    st.on_wake_woke()
    snap = st.snapshot()["wake_gate"]
    assert snap["state"] == "LISTENING"
    assert snap["last_wake_at"] == 30.0
    assert snap["wake_count"] == 1

    st.on_wake_closed()
    snap = st.snapshot()["wake_gate"]
    assert snap["state"] == "DORMANT"
    assert snap["close_count"] == 1


def test_audio_events_update_snapshot():
    clock = FakeClock(40.0)
    st = DebugStatus(clock=clock)
    st.on_device_audio()
    st.on_tts_state(True)
    snap = st.snapshot()["audio"]
    assert snap["last_device_audio_at"] == 40.0
    assert snap["tts_active"] is True


def test_face_tracking_section_comes_from_the_provider():
    st = DebugStatus()
    assert st.snapshot()["face_tracking"] is None
    st.face_tracking_provider = lambda: {"tracker_running": True}
    assert st.snapshot()["face_tracking"] == {"tracker_running": True}


def test_tts_sources_do_not_clear_another_active_speaker():
    st = DebugStatus()
    st.on_tts_state(True, source="local")
    st.on_tts_state(True, source="gemini")
    st.on_tts_state(False, source="local")
    assert st.tts_active is True
    st.on_tts_state(False, source="gemini")
    assert st.tts_active is False


# --- recent --------------------------------------------------------------


def test_recent_tool_calls_keep_latest_10_newest_first():
    st = DebugStatus(clock=FakeClock())
    for i in range(RECENT_LIMIT + 5):
        st.record_tool_call(f"tool-{i}", ok=True)
    calls = st.snapshot()["recent"]["tool_calls"]
    assert len(calls) == RECENT_LIMIT
    assert calls[0]["name"] == f"tool-{RECENT_LIMIT + 4}"


def test_recent_tool_call_error_is_recorded():
    st = DebugStatus()
    st.record_tool_call("media_control", ok=False, error="device offline")
    entry = st.snapshot()["recent"]["tool_calls"][0]
    assert entry["ok"] is False
    assert entry["error"] == "device offline"


def test_recent_transcripts_keep_latest_10_and_skip_empty():
    st = DebugStatus(clock=FakeClock())
    st.record_transcript("")
    for i in range(RECENT_LIMIT + 2):
        st.record_transcript(f"回应 {i}")
    entries = st.snapshot()["recent"]["transcripts"]
    assert len(entries) == RECENT_LIMIT
    assert entries[0]["chars"] == len(f"回应 {RECENT_LIMIT + 1}")
    assert "text" not in entries[0]
    assert "回应" not in json.dumps(entries)


def test_snapshot_is_json_serializable_with_required_fields():
    st = DebugStatus()
    snap = st.snapshot()
    json.dumps(snap)  # does not raise
    assert set(snap) >= {"generated_at", "device", "gemini", "wake_gate", "audio", "recent"}


# --- HTTP endpoints -------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_status_endpoint_returns_snapshot():
    st = DebugStatus(clock=FakeClock(123.0))
    st.on_device_connected("dev-9")
    app = create_capture_app(debug_status=st)

    response = await handle_debug_status(FakeAppRequest(app))
    assert response.status == 200
    body = json.loads(response.text)
    assert body["device"]["connected"] is True
    assert body["device"]["device_id"] == "dev-9"
    assert body["generated_at"] == 123.0


@pytest.mark.asyncio
async def test_debug_panel_endpoint_returns_self_refreshing_html():
    app = create_capture_app(debug_status=DebugStatus())
    response = await handle_debug_panel(FakeAppRequest(app))
    assert response.status == 200
    assert response.content_type == "text/html"
    assert "/debug/status" in response.text
    assert "3000" in response.text  # polls every 3 s
    assert "StackChan 状态面板" in response.text
    assert "Token 用量" in response.text


def test_capture_app_defaults_to_singleton_status():
    reset_debug_status()
    app = create_capture_app()
    assert app[DEBUG_STATUS_KEY] is get_debug_status()


# --- events from the components -----------------------------------------------------------


class ScriptedSpotter:
    available = True

    def __init__(self, hits: list[bool]) -> None:
        self.hits = list(hits)

    def detect(self, _pcm: bytes) -> bool:
        return self.hits.pop(0) if self.hits else False

    def reset(self) -> None:
        pass


class AudioSinkBridge:
    def __init__(self) -> None:
        self.audio_sent: list[bytes] = []

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_sent.append(pcm)


@pytest.mark.asyncio
async def test_proxy_wake_and_close_events_reach_debug_status():
    from stackchan_mcp.gemini_voice_proxy import GeminiVoiceProxy
    from stackchan_mcp.wake_gate import WakeGate

    now = 0.0
    st = DebugStatus(clock=FakeClock(5.0))
    proxy = GeminiVoiceProxy(debug_status=st)
    proxy._bridge = AudioSinkBridge()
    gate = WakeGate(
        kws=ScriptedSpotter([True, False]),
        idle_s=1.0,
        activity_rms_threshold=1000.0,
        clock=lambda: now,
    )
    proxy._wake_gate = gate

    await proxy._forward_gated_device_pcm(b"\x00\x00")
    assert st.snapshot()["wake_gate"]["state"] == "LISTENING"
    assert st.snapshot()["wake_gate"]["wake_count"] == 1

    now = 1.5  # past idle_s, the gate closes the window
    await proxy._forward_gated_device_pcm(b"\x00\x00")
    assert st.snapshot()["wake_gate"]["state"] == "DORMANT"
    assert st.snapshot()["wake_gate"]["close_count"] == 1


def test_bridge_tool_call_logging_reaches_debug_status():
    from stackchan_mcp.gemini_live_bridge import GeminiLiveBridge

    st = DebugStatus()
    bridge = GeminiLiveBridge(object(), api_key="k", debug_status=st)
    bridge._log_tool_result("move_head", {"yaw": 0}, {"ok": True, "result": None})
    bridge._log_tool_result("set_avatar", {}, {"ok": False, "error": "offline"})

    calls = st.snapshot()["recent"]["tool_calls"]
    assert [c["name"] for c in calls] == ["set_avatar", "move_head"]
    assert calls[0]["error"] == "offline"
