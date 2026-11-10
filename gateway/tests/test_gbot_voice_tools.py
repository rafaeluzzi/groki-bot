# SPDX-License-Identifier: MIT
import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge


class Executor:
    def __init__(self):
        self.execute = AsyncMock(return_value={"ok": True})
        self.close = AsyncMock()
        self.observe_user_input = AsyncMock()

    def declarations(self):
        return [
            {
                "name": "self.tracking.start",
                "description": "Track",
                "parameters": {"type": "object"},
            },
            {
                "name": "ask_grokbot",
                "description": "Delegate",
                "parameters": {
                    "type": "object",
                    "properties": {"task": {"type": "string"}},
                    "required": ["task"],
                },
            },
        ]

    def instructions(self, **kwargs):
        assert kwargs == {"model_search": False}
        return "Shared local instructions"


def make(monkeypatch, enabled=True):
    monkeypatch.setenv("STACKCHAN_TOOL_BOT", "desktop" if enabled else "")
    monkeypatch.setenv("STACKCHAN_TOOL_BOT_ID", "agent")
    b = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        agent_id="agent",
        tool_executor=Executor(),
    )
    b._send = AsyncMock()
    b._native = SimpleNamespace(ready=True, close=AsyncMock())
    b.running = True
    b._configure_tools(
        {
            "tools": [
                {
                    "type": "function",
                    "name": "send_task",
                    "parameters": {
                        "type": "object",
                        "properties": {"request": {"type": "string"}},
                        "required": ["request"],
                    },
                }
            ]
        }
    )
    return b


def _reply(text, entry="e1"):
    import time

    return {
        "type": "async_update.add",
        "event_id": entry,
        "desktop_entry": entry,
        "async_update": {
            "topic": "work_landed",
            "payload": [
                {
                    "message": {"type": "text", "content": text},
                    "timestampMs": int(time.time() * 1000),
                }
            ],
        },
    }


async def _finish_audio(b, response_id="r1"):
    import base64

    await b._handle_event(
        {
            "type": "response.created",
            "response": {"id": response_id, "metadata": {"trigger": "async_update"}},
        }
    )
    await b._handle_event(
        {
            "type": "response.audio.delta",
            "response_id": response_id,
            "delta": base64.b64encode(b"\x00\x00").decode(),
        }
    )
    await b._handle_event(
        {"type": "response.done", "response": {"id": response_id, "status": "completed"}}
    )


@pytest.mark.asyncio
async def test_advertised_alias_dispatches_original_local_name(monkeypatch):
    b = make(monkeypatch)
    name = next(
        name
        for name, original in b._local_tools.items()
        if original == "self.tracking.start"
    )
    assert "." not in name
    await b._tool({"name": name, "call_id": "one", "arguments": "{}"})
    b._executor.execute.assert_awaited_once_with("self.tracking.start", {})
    await b.stop()


@pytest.mark.asyncio
async def test_native_tool_once_per_real_user_and_no_result_recursion(monkeypatch):
    b = make(monkeypatch)
    b._local = AsyncMock(return_value={"receipt": "accepted"})
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    event = {"name": "send_task", "call_id": "one", "arguments": '{"request":"lookup"}'}
    await b._tool(event)
    await b._tool(event)
    assert b._local.await_count == 1
    assert b._local.call_args.args[1]["name"] == "send_task"
    assert b._local.call_args.args[1]["input"] == {"request": "lookup"}
    await b._native_update(
        {
            "type": "async_update.add",
            "async_update": {"topic": "work_landed", "payload": []},
        },
        False,
    )
    await b._tool({**event, "call_id": "two"})
    assert b._local.await_count == 1
    b._executor.execute.assert_not_awaited()
    await b.stop()


@pytest.mark.asyncio
async def test_slow_tool_does_not_block_interrupt_and_stop_drops_late_results(
    monkeypatch,
):
    b = make(monkeypatch)
    entered = asyncio.Event()

    async def slow(*args):
        entered.set()
        await asyncio.Event().wait()

    b._executor.execute = slow
    name = next(iter(b._local_tools))
    await b._handle_event(
        {
            "type": "response.function_call_arguments.done",
            "name": name,
            "call_id": "slow",
            "arguments": "{}",
        }
    )
    await entered.wait()
    await asyncio.wait_for(
        b._handle_event({"type": "input_audio_buffer.speech_started"}), 0.2
    )
    await b.stop()
    count = b._send.await_count
    await b._native_update({"type": "async_update.add"}, False)
    assert b._send.await_count == count


@pytest.mark.asyncio
async def test_function_result_waits_for_active_response_before_create(monkeypatch):
    b = make(monkeypatch)
    await b._handle_event({"type": "response.created", "response": {"id": "active"}})
    await b._tool(
        {"name": next(iter(b._local_tools)), "call_id": "tool", "arguments": "{}"}
    )
    assert not any(
        c.args[0]["type"] == "response.create" for c in b._send.call_args_list
    )
    await b._handle_event({"type": "response.done", "response": {"id": "active"}})
    assert any(c.args[0]["type"] == "response.create" for c in b._send.call_args_list)
    await b.stop()


@pytest.mark.asyncio
async def test_internal_speech_does_not_grant_desktop_permission(monkeypatch):
    b = make(monkeypatch)
    b._local = AsyncMock()
    await b.inject_user_text("read a task result")
    await b._tool(
        {"name": "send_task", "call_id": "one", "arguments": '{"request":"do more"}'}
    )
    b._local.assert_not_awaited()
    await b.stop()


def test_desktop_requires_explicit_matching_target(monkeypatch):
    b = make(monkeypatch, enabled=False)
    assert "ask_grokbot" not in b._local_tools
    assert "send_task" not in b._remote_tools


def test_alias_collision_fails_closed(monkeypatch):
    b = make(monkeypatch)
    b._executor.declarations = lambda: [
        {"name": "self.a", "parameters": {}},
        {"name": "self_a", "parameters": {}},
    ]
    with pytest.raises(ValueError, match="collision"):
        b._configure_tools({})


def test_diagnostics_disable_even_native_end_and_extra_tools(monkeypatch):
    b = make(monkeypatch)
    b._local_tools_enabled = False
    b._on_end_conversation = AsyncMock()
    monkeypatch.setenv("STACKCHAN_GBOT_VOICE_TOOLS", "1")
    assert (
        b._configure_tools(
            {"tools": [{"name": "end_the_call"}, {"name": "search_conversations"}]}
        )
        == []
    )


@pytest.mark.asyncio
async def test_native_update_waits_for_user_turn_and_never_creates_response(
    monkeypatch,
):
    b = make(monkeypatch)
    await b._handle_event({"type": "response.created", "response": {"id": "speech"}})
    update = {
        "type": "async_update.add",
        "async_update": {"topic": "work_landed", "payload": []},
    }
    await b._native_update(update, False)
    b._send.assert_not_awaited()
    await b._handle_event({"type": "response.done", "response": {"id": "speech"}})
    b._send.assert_awaited_once_with(update)
    await b.stop()


@pytest.mark.asyncio
async def test_native_task_survives_ordinary_interrupt(monkeypatch):
    b = make(monkeypatch)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def send(*args):
        entered.set()
        await finish.wait()
        return {"receipt": "accepted"}

    b._local = send
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._handle_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "send_task",
            "call_id": "task",
            "arguments": '{"request":"fixture"}',
        }
    )
    await entered.wait()
    await b.interrupt()
    assert any(not task.done() for task in b._tools)
    finish.set()
    await asyncio.gather(*b._tools)
    assert any(
        call.args[0]["type"] == "conversation.item.create"
        for call in b._send.call_args_list
    )
    await b.stop()


def test_executor_factory_gets_live_callback_options(monkeypatch):
    b = make(monkeypatch)
    b._executor = None
    observed = {}

    def factory(**options):
        observed.update(options)
        return Executor()

    b._executor_factory = factory
    b._configure_tools({})
    assert observed["announce_result"] == b._announce_result
    assert observed["cancel_idle"] == b._cancel_idle
    assert "wake_gate_state_getter" in observed


@pytest.mark.asyncio
async def test_idle_callback_uses_same_end_hook_and_keepalive_does_not_cancel(
    monkeypatch,
):
    b = make(monkeypatch)
    b._on_end_conversation = AsyncMock()
    b._conversation_idle_timeout_s = 0.01
    await b._finish_turn()
    await b.send_keepalive_audio(b"\0\0" * 160)
    await asyncio.sleep(0.03)
    b._on_end_conversation.assert_awaited_once()
    await b.stop()


@pytest.mark.asyncio
async def test_real_input_cancels_idle_and_is_forwarded_to_extensions(monkeypatch):
    b = make(monkeypatch)
    b._on_end_conversation = AsyncMock()
    b._conversation_idle_timeout_s = 0.01
    await b._finish_turn()
    await b.send_text("real request")
    await asyncio.sleep(0.03)
    b._on_end_conversation.assert_not_awaited()
    b._executor.observe_user_input.assert_awaited_once_with(
        "real request", finished=True
    )
    await b.stop()


@pytest.mark.asyncio
async def test_native_tool_accepts_schema_allowed_extra_fields_without_forwarding_them(
    monkeypatch,
):
    b = make(monkeypatch)
    b._local = AsyncMock(
        return_value={"kind": "served", "output": {"status": "accepted"}}
    )
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {
            "name": "send_task",
            "call_id": "one",
            "arguments": '{"request":"offline fixture","language":"zh"}',
        }
    )
    b._local.assert_awaited_once()
    assert b._local.call_args.args[1]["input"] == {"request": "offline fixture"}
    await b.stop()


@pytest.mark.asyncio
async def test_native_tool_obeys_explicit_additional_properties_false(monkeypatch):
    b = make(monkeypatch)
    b._tool_schemas["send_task"]["additionalProperties"] = False
    b._local = AsyncMock()
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {
            "name": "send_task",
            "call_id": "one",
            "arguments": '{"request":"offline fixture","language":"zh"}',
        }
    )
    b._local.assert_not_awaited()
    await b.stop()


@pytest.mark.asyncio
async def test_tool_diagnostics_do_not_log_arguments_ids_or_unknown_names(
    monkeypatch, caplog
):
    import logging

    b = make(monkeypatch)
    caplog.set_level(logging.INFO, logger="stackchan_mcp.gbot_voice_bridge")
    await b._handle_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "UNTRUSTED_NAME_SENTINEL",
            "call_id": "PRIVATE_ID_SENTINEL",
            "arguments": '{"request":"PRIVATE_TASK_SENTINEL"}',
        }
    )
    assert b.metrics["tool_events"] == {"other": 1}
    assert b.metrics["tool_outcomes"] == {"other:undeclared": 1}
    assert "SENTINEL" not in caplog.text
    await b.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native,expected",
    [
        ({"kind": "served", "output": {"relay": "accepted"}}, "accepted"),
        (
            {
                "kind": "served",
                "output": {"relay": "refused", "spoken": "Cannot accept this task."},
            },
            "refused",
        ),
        (
            {
                "kind": "served",
                "output": {
                    "relay": "unreachable",
                    "spoken": "Desktop is unreachable.",
                    "errorClass": "network",
                },
            },
            "unreachable",
        ),
        ({"kind": "invalid_input", "output": {"relay": "accepted"}}, "unreachable"),
        ({"kind": "unknown_tool"}, "unreachable"),
        ({"kind": "served", "output": {"ok": True}}, "unreachable"),
    ],
)
async def test_native_task_uses_client_relay_outcome_not_http_success(
    monkeypatch, native, expected
):
    import json

    b = make(monkeypatch)
    b._local = AsyncMock(return_value=native)
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {
            "name": "send_task",
            "call_id": "task",
            "arguments": '{"request":"offline fixture"}',
        }
    )
    output = next(
        c.args[0]["item"]["output"]
        for c in b._send.call_args_list
        if c.args[0]["type"] == "conversation.item.create"
    )
    parsed = json.loads(output)
    assert parsed["relay"] == expected
    assert "kind" not in parsed and "output" not in parsed
    if expected != "accepted":
        assert parsed.get("spoken")
    await b.stop()


@pytest.mark.asyncio
async def test_native_task_transport_failure_is_not_accepted(monkeypatch):
    import json

    b = make(monkeypatch)
    b._local = AsyncMock(side_effect=RuntimeError("PRIVATE_FAILURE_SENTINEL"))
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {
            "name": "send_task",
            "call_id": "task",
            "arguments": '{"request":"offline fixture"}',
        }
    )
    raw = next(
        c.args[0]["item"]["output"]
        for c in b._send.call_args_list
        if c.args[0]["type"] == "conversation.item.create"
    )
    assert json.loads(raw)["relay"] == "unreachable"
    assert "PRIVATE_FAILURE_SENTINEL" not in raw
    await b.stop()


def test_grok_advertises_native_desktop_tool_without_local_alias(monkeypatch):
    b = make(monkeypatch)
    assert "send_task" in b._remote_tools
    assert "ask_grokbot" not in b._local_tools


def test_native_end_replaces_duplicate_local_end_only_when_available(monkeypatch):
    b = make(monkeypatch)
    b._on_end_conversation = AsyncMock()
    original = b._executor.declarations()
    b._executor.declarations = lambda: [
        *original,
        {"name": "end_conversation", "parameters": {"type": "object"}},
    ]
    declarations = b._configure_tools(
        {"tools": [{"name": "end_the_call", "parameters": {"type": "object"}}]}
    )
    names = {item["name"] for item in declarations}
    assert "end_the_call" in names and "end_conversation" not in names
    declarations = b._configure_tools({"tools": []})
    assert "end_conversation" in {item["name"] for item in declarations}


@pytest.mark.parametrize("legacy_flag", ["0", "1"])
def test_known_native_search_is_enabled_without_opening_unknown_tools(
    monkeypatch, legacy_flag
):
    b = make(monkeypatch)
    monkeypatch.setenv("STACKCHAN_GBOT_VOICE_TOOLS", legacy_flag)
    b._configure_tools(
        {
            "tools": [
                {"name": "send_task", "parameters": {"type": "object"}},
                {"name": "search_conversations", "parameters": {"type": "object"}},
                {"name": "unknown_remote_action", "parameters": {"type": "object"}},
            ]
        }
    )
    assert set(b._remote_tools) == {"send_task", "search_conversations"}


def test_grok_drops_legacy_wrapper_routing_without_changing_local_instructions(
    monkeypatch,
):
    b = make(monkeypatch)
    source = "Use move_head for hardware.\nask_grokbot returns at once. Say sent immediately.\nUse ask_grokbot(task) for tasks."
    b._executor.instructions = lambda **kwargs: source
    instructions = b._provider_tool_instructions()
    assert "ask_grokbot" not in instructions and "returns at once" not in instructions
    assert "Use move_head for hardware." in instructions
    assert "relay=accepted" in instructions
    assert "Do not promise to look it up later" in instructions
    assert b._executor.instructions() == source


def test_grok_omits_entire_legacy_desktop_instruction_section(monkeypatch):
    b = make(monkeypatch)
    b._executor.instructions = lambda **kwargs: (
        "Hardware instructions.\n\n## Handing tasks to desktop\n\n"
        "Call ask_grokbot(task).\nWhen desktop answers you get a system notice.\n\n"
        "Other local tool instructions."
    )
    instructions = b._provider_tool_instructions()
    assert "system notice" not in instructions
    assert "Hardware instructions." in instructions
    assert "Other local tool instructions." in instructions


@pytest.mark.asyncio
async def test_native_update_without_model_response_does_not_block_later_completion(
    monkeypatch,
):
    b = make(monkeypatch)
    update = {
        "type": "async_update.add",
        "async_update": {"topic": "work_landed", "payload": []},
    }
    await b._native_update(update, False)
    await b._native_update(update, False)
    assert b._send.await_count == 2
    assert b._response_requested is False
    assert not b._pending_updates
    await b.stop()


@pytest.mark.asyncio
async def test_completion_queued_during_user_speech_flushes_at_speech_stop(monkeypatch):
    b = make(monkeypatch)
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    update = {
        "type": "async_update.add",
        "async_update": {"topic": "work_landed", "payload": []},
    }
    await b._native_update(update, False)
    b._send.assert_not_awaited()
    await b._handle_event({"type": "input_audio_buffer.speech_stopped"})
    b._send.assert_awaited_once_with(update)
    assert not b._pending_updates
    await b.stop()


@pytest.mark.asyncio
async def test_native_notification_waits_for_already_requested_manual_response(
    monkeypatch,
):
    b = make(monkeypatch)
    b._response_requested = True
    update = {
        "type": "async_update.add",
        "async_update": {"topic": "work_landed", "payload": []},
    }
    await b._native_update(update, False)
    b._send.assert_not_awaited()
    assert b._response_requested is True
    assert len(b._pending_updates) == 1
    await b._handle_event({"type": "response.created", "response": {"id": "manual"}})
    await b._handle_event({"type": "response.done", "response": {"id": "manual"}})
    b._send.assert_awaited_once_with(update)
    await b.stop()


@pytest.mark.asyncio
async def test_native_delivery_and_response_logs_are_content_free(monkeypatch, caplog):
    import logging

    b = make(monkeypatch)
    caplog.set_level(logging.INFO, logger="stackchan_mcp.gbot_voice_bridge")
    update = {
        "type": "async_update.add",
        "event_id": "PRIVATE_EVENT_SENTINEL",
        "async_update": {
            "topic": "work_landed",
            "payload": ["PRIVATE_RESULT_SENTINEL"],
        },
    }
    await b._native_update(update, False)
    await b._handle_event(
        {
            "type": "response.created",
            "response": {
                "id": "PRIVATE_RESPONSE_SENTINEL",
                "metadata": {"trigger": "async_update"},
            },
        }
    )
    await b._handle_event(
        {
            "type": "response.audio.delta",
            "response_id": "PRIVATE_RESPONSE_SENTINEL",
            "delta": "AAA=",
        }
    )
    assert b.metrics["native_updates"]["work_landed:sent"] == 1
    assert b.metrics["response_triggers"]["async_update"] == 1
    assert "state=first_audio trigger=async_update" in caplog.text
    assert "SENTINEL" not in caplog.text
    await b.stop()


@pytest.mark.asyncio
async def test_existing_manual_request_precedes_native_without_starvation(monkeypatch):
    b = make(monkeypatch)
    b._pending_response = True
    update = {
        "type": "async_update.add",
        "async_update": {"topic": "work_landed", "payload": []},
    }
    b._pending_updates.append(update)
    await b._flush_pending()
    b._send.assert_awaited_once_with({"type": "response.create"})
    assert b._response_requested is True and not b._pending_response
    assert b._pending_updates == [update]
    await b._handle_event({"type": "response.created", "response": {"id": "manual"}})
    await b._handle_event({"type": "response.done", "response": {"id": "manual"}})
    assert [call.args[0]["type"] for call in b._send.call_args_list] == [
        "response.create",
        "async_update.add",
    ]
    assert not b._pending_updates and not b._response_requested
    await b.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [("send_task", '{"request":"fixture"}'), ("search_conversations", "{}")],
)
async def test_native_not_ready_rejects_desktop_tools_without_post(
    monkeypatch, name, args
):
    from types import SimpleNamespace

    b = make(monkeypatch)
    b._remote_tools["search_conversations"] = {"parameters": {"type": "object"}}
    b._native = SimpleNamespace(ready=False, close=AsyncMock())
    b._local = AsyncMock()
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool({"name": name, "call_id": "offline", "arguments": args})
    b._local.assert_not_awaited()
    assert b.metrics["tool_outcomes"][name + ":native_not_ready"] == 1
    await b.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [("send_task", '{"request":"fixture"}'), ("search_conversations", "{}")],
)
async def test_unavailable_desktop_has_readable_english_without_diagnostic_code(
    monkeypatch, name, args
):
    import json

    b = make(monkeypatch)
    b._remote_tools["search_conversations"] = {"parameters": {"type": "object"}}
    b._native.ready = False
    await b._tool({"name": name, "call_id": "offline", "arguments": args})
    raw = next(
        call.args[0]["item"]["output"]
        for call in b._send.call_args_list
        if call.args[0]["type"] == "conversation.item.create"
    )
    output = json.loads(raw)
    assert output["spoken"] == "Desktop tasks are down right now."
    assert "failureCode" not in output and "native_not_ready" not in raw
    assert b.metrics["tool_outcomes"][name + ":native_not_ready"] == 1
    assert "spoken" in b._provider_tool_instructions()
    assert "Do not promise to look it up later" in b._provider_tool_instructions()
    await b.stop()


@pytest.mark.asyncio
async def test_login_rejected_desktop_line_names_the_cause(monkeypatch):
    import json

    b = make(monkeypatch)
    b._native = SimpleNamespace(
        ready=False,
        close=AsyncMock(),
        health=lambda: {"reason": "login_rejected", "ready": False},
    )
    await b._tool({"name": "send_task", "call_id": "rejected", "arguments": '{"request":"fixture"}'})
    raw = next(
        call.args[0]["item"]["output"]
        for call in b._send.call_args_list
        if call.args[0]["type"] == "conversation.item.create"
    )
    spoken = json.loads(raw)["spoken"]
    assert spoken == "Desktop is down because the Grok Bot login was rejected."
    assert "401" not in spoken
    await b.stop()


@pytest.mark.asyncio
async def test_unavailable_send_task_logs_channel_down(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    b = make(monkeypatch)
    b._native.ready = False
    await b._tool(
        {"name": "send_task", "call_id": "offline", "arguments": '{"request":"events nearby"}'}
    )
    line = next(r.getMessage() for r in caplog.records if r.message.startswith("desktop task"))
    assert "outcome=channel_down" in line
    assert "waited_ms=0" in line
    assert "events nearby" not in line
    await b.stop()


@pytest.mark.asyncio
async def test_accepted_task_is_delivered_when_work_lands(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    b = make(monkeypatch)
    b._local = AsyncMock(
        return_value={"kind": "served", "output": {"relay": "accepted"}}
    )
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {"name": "send_task", "call_id": "task", "arguments": '{"request":"events nearby"}'}
    )
    await b._handle_event({"type": "input_audio_buffer.speech_stopped"})
    b._pending_response = False
    b._response_requested = False
    await b._native_update(_reply("Saturday market", "e-market"), False, track=True)
    assert not any("outcome=delivered" in r.message for r in caplog.records)
    await _finish_audio(b)
    line = next(r.getMessage() for r in caplog.records if "outcome=" in r.message)
    assert "outcome=delivered" in line
    assert "hop=spoken" in caplog.text
    assert "events nearby" not in line and "Saturday" not in line
    await b.stop()


@pytest.mark.asyncio
async def test_unlabeled_reply_is_spoken_and_quiet_waits_for_playback(monkeypatch, caplog):
    # 13e84dcebc96: the answer's response was trigger=other, so it never logged
    # spoken, and quiet had already started at queue time.
    import logging

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    release = asyncio.Event()

    async def playback():
        await release.wait()

    b = make(monkeypatch)
    b._on_turn_complete = playback
    b._desktop_tasks.quiet_s = 0.05
    b._local = AsyncMock(return_value={"kind": "served", "output": {"relay": "accepted"}})
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {"name": "send_task", "call_id": "ash", "arguments": '{"request":"Asheville forecast"}'}
    )
    await b._handle_event({"type": "input_audio_buffer.speech_stopped"})
    b._pending_response = False
    b._response_requested = False
    await b._native_update(_reply("Tuesday in Asheville stays dry.", "t59s1"), False, track=True)
    task_id = next(iter(b._desktop_tasks._open))
    import base64

    await b._handle_event(
        {"type": "response.created", "response": {"id": "r-other", "metadata": {"trigger": "other"}}}
    )
    await b._handle_event(
        {
            "type": "response.audio.delta",
            "response_id": "r-other",
            "delta": base64.b64encode(b"\x00\x00").decode(),
        }
    )
    await b._handle_event(
        {"type": "response.done", "response": {"id": "r-other", "status": "completed"}}
    )
    assert any(f"id={task_id} hop=spoken" in r.message for r in caplog.records)
    assert any(f"id={task_id}" in r.message and "outcome=delivered" in r.message for r in caplog.records)
    assert task_id in b._desktop_tasks._open
    assert b._desktop_tasks._open[task_id].quiet is None
    release.set()
    await b._turn_task
    await asyncio.sleep(0.12)
    assert task_id not in b._desktop_tasks._open
    messages = [r.message for r in caplog.records]
    spoken_at = next(i for i, m in enumerate(messages) if f"id={task_id} hop=spoken" in m)
    quiet_at = next(i for i, m in enumerate(messages) if f"id={task_id} hop=quiet" in m)
    assert spoken_at < quiet_at
    await b.stop()


@pytest.mark.asyncio
async def test_user_speech_does_not_take_a_pending_desktop_reply(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    b = make(monkeypatch)
    b._local = AsyncMock(return_value={"kind": "served", "output": {"relay": "accepted"}})
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {"name": "send_task", "call_id": "ash", "arguments": '{"request":"Asheville forecast"}'}
    )
    b._pending_response = False
    b._response_requested = False
    b._response_in_progress = False
    b._user_speaking = False
    await b._native_update(_reply("Tuesday in Asheville stays dry.", "t59s1"), False, track=True)
    import base64

    await b._handle_event(
        {
            "type": "response.created",
            "response": {"id": "vad", "metadata": {"trigger": "server_vad"}},
        }
    )
    await b._handle_event(
        {
            "type": "response.audio.delta",
            "response_id": "vad",
            "delta": base64.b64encode(b"\x00\x00").decode(),
        }
    )
    await b._handle_event({"type": "response.done", "response": {"id": "vad", "status": "completed"}})
    assert not any("hop=spoken" in r.message for r in caplog.records)
    assert b._async_sent
    await b.stop()


@pytest.mark.asyncio
async def test_accepted_task_speaks_a_failure_after_the_turn(monkeypatch, caplog):
    import asyncio
    import logging

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    b = make(monkeypatch)
    b._desktop_tasks.timeout_s = 0.02
    b._response_in_progress = False
    b._local = AsyncMock(
        return_value={"kind": "served", "output": {"relay": "accepted"}}
    )
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {"name": "send_task", "call_id": "task", "arguments": '{"request":"events nearby"}'}
    )
    b._user_speaking = False
    b._pending_response = False
    b._response_requested = False
    await asyncio.sleep(0.08)
    updates = [
        call.args[0]
        for call in b._send.call_args_list
        if call.args[0].get("type") == "async_update.add"
    ]
    assert updates
    content = updates[-1]["async_update"]["payload"][0]["message"]["content"]
    assert content == "That one didn't come back in time."
    line = next(r.getMessage() for r in caplog.records if "outcome=" in r.message)
    assert "outcome=timeout" in line
    assert "events nearby" not in line
    await b.stop()


class _GraceChannel:
    def __init__(self, *, recent):
        self.ready = False
        self.recent = recent

    def within_reconnect_grace(self):
        return self.recent

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_reconnect_grace_sends_when_the_channel_returns(monkeypatch, caplog):
    import asyncio
    import logging

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    b = make(monkeypatch)
    channel = _GraceChannel(recent=True)
    b._native = channel
    b._local = AsyncMock(return_value={"kind": "served", "output": {"relay": "accepted"}})

    async def recover():
        await asyncio.sleep(0.08)
        channel.ready = True

    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    waiter = asyncio.create_task(recover())
    await b._tool(
        {"name": "send_task", "call_id": "blip", "arguments": '{"request":"weekend weather"}'}
    )
    await waiter
    await b._handle_event({"type": "input_audio_buffer.speech_stopped"})
    b._pending_response = False
    b._response_requested = False
    b._local.assert_awaited()
    raw = next(
        c.args[0]["item"]["output"]
        for c in b._send.call_args_list
        if c.args[0]["type"] == "conversation.item.create"
    )
    assert "Desktop tasks are down" not in raw
    await b._native_update(_reply("Warm and dry.", "e-warm"), False, track=True)
    await _finish_audio(b, "warm-audio")
    line = next(r.getMessage() for r in caplog.records if "outcome=delivered" in r.message)
    waited = int(line.split("waited_ms=")[1])
    assert 0 < waited < 8000
    await b.stop()


@pytest.mark.asyncio
async def test_long_outage_fails_without_waiting(monkeypatch, caplog):
    import logging
    import time

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    b = make(monkeypatch)
    b._native = _GraceChannel(recent=False)
    b._local = AsyncMock()
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    started = time.monotonic()
    await b._tool(
        {"name": "send_task", "call_id": "down", "arguments": '{"request":"weekend weather"}'}
    )
    assert time.monotonic() - started < 0.4
    b._local.assert_not_awaited()
    line = next(r.getMessage() for r in caplog.records if r.message.startswith("desktop task"))
    assert "outcome=channel_down" in line
    assert "waited_ms=0" in line
    await b.stop()


@pytest.mark.asyncio
async def test_grace_that_does_not_recover_logs_the_wait(monkeypatch, caplog):
    import logging

    import stackchan_mcp.gbot_voice_bridge as bridge

    monkeypatch.setattr(bridge, "RECONNECT_GRACE_S", 0.12)
    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    b = make(monkeypatch)
    b._native = _GraceChannel(recent=True)
    b._local = AsyncMock()
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {"name": "send_task", "call_id": "stuck", "arguments": '{"request":"weekend weather"}'}
    )
    b._local.assert_awaited()
    line = next(r.getMessage() for r in caplog.records if "outcome=channel_down" in r.message)
    waited = int(line.split("waited_ms=")[1])
    assert waited >= 100
    raw = next(
        c.args[0]["item"]["output"]
        for c in b._send.call_args_list
        if c.args[0]["type"] == "conversation.item.create"
    )
    assert "Desktop tasks are down right now." in raw
    await b.stop()


@pytest.mark.asyncio
async def test_send_during_warm_is_tried_even_if_stream_stays_down(monkeypatch, caplog):
    import logging
    import time

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    b = make(monkeypatch)
    b._native = _GraceChannel(recent=False)
    b._local = AsyncMock(return_value={"kind": "served", "output": {"relay": "accepted"}})

    async def warm():
        await asyncio.sleep(2)

    b._warm_task = asyncio.create_task(warm())
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    started = time.monotonic()
    await b._tool(
        {"name": "send_task", "call_id": "cary", "arguments": '{"request":"indoor events in Cary"}'}
    )
    assert time.monotonic() - started < 0.4
    b._local.assert_awaited()
    assert b._native.ready is False
    assert any("hop=accepted" in r.message for r in caplog.records)
    assert not any("outcome=channel_down" in r.message for r in caplog.records)
    b._warm_task.cancel()
    await b.stop()


@pytest.mark.asyncio
async def test_bridge_resends_a_silent_no_reply_once(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    b = make(monkeypatch)
    b._desktop_tasks.retry_s = 0.05
    b._desktop_tasks.timeout_s = 2
    b._local = AsyncMock(return_value={"kind": "served", "output": {"relay": "accepted"}})
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {"name": "send_task", "call_id": "once", "arguments": '{"request":"Durham events"}'}
    )
    await asyncio.sleep(0.15)
    tool_calls = [c for c in b._local.await_args_list if c.args[0] == "/tool"]
    assert len(tool_calls) == 2
    assert tool_calls[1].args[1]["input"]["request"] == "Durham events"
    assert any("hop=resend" in r.message for r in caplog.records)
    await asyncio.sleep(0.15)
    tool_calls = [c for c in b._local.await_args_list if c.args[0] == "/tool"]
    assert len(tool_calls) == 2
    await b.stop()


@pytest.mark.asyncio
async def test_bridge_does_not_resend_after_a_reply(monkeypatch):
    b = make(monkeypatch)
    b._desktop_tasks.retry_s = 0.05
    b._desktop_tasks.timeout_s = 2
    b._local = AsyncMock(return_value={"kind": "served", "output": {"relay": "accepted"}})
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {"name": "send_task", "call_id": "done", "arguments": '{"request":"Durham events"}'}
    )
    await b._native_update(_reply("Durham has a busy weekend.", "e-real"), False, track=True)
    await asyncio.sleep(0.15)
    tool_calls = [c for c in b._local.await_args_list if c.args[0] == "/tool"]
    assert len(tool_calls) == 1
    await b.stop()


def _updates(b):
    queued = [
        item for item in b._pending_updates if item.get("type") == "async_update.add"
    ]
    sent = [
        call.args[0]
        for call in b._send.call_args_list
        if call.args[0].get("type") == "async_update.add"
    ]
    return queued + sent


@pytest.mark.asyncio
async def test_holding_line_and_answer_are_each_spoken_once_while_open(monkeypatch):
    b = make(monkeypatch)
    b._local = AsyncMock(return_value={"kind": "served", "output": {"relay": "accepted"}})
    b._desktop_tasks.quiet_s = 30
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {"name": "send_task", "call_id": "once", "arguments": '{"request":"Durham events"}'}
    )
    await b._native_update(
        _reply("Looking up Durham events for November 14 and 15 now.", "e-hold"),
        False,
        track=True,
    )
    await b._native_update(
        _reply("Looking up Durham events for November 14 and 15 now.", "e-hold"),
        False,
        track=True,
    )
    await b._native_update(_reply("Durham has a packed weekend.", "e-real"), False, track=True)
    texts = [
        item["async_update"]["payload"][0]["message"]["content"] for item in _updates(b)
    ]
    assert texts == [
        "Looking up Durham events for November 14 and 15 now.",
        "Durham has a packed weekend.",
    ]
    assert len(b._desktop_tasks._open) == 1
    await b.stop()


@pytest.mark.asyncio
async def test_nudge_and_transcript_copy_are_spoken_once(monkeypatch):
    from stackchan_mcp.gbot_native_channel import update_event

    b = make(monkeypatch)
    b._desktop_tasks.start("Saturday market")
    signal = {
        "channel": "box-signal",
        "payload": {"agentId": "agent", "callId": b.call_id, "kind": "outcome"},
    }
    reply = {
        "channel": "box-reply",
        "payload": {
            "agentId": "agent",
            "callId": b.call_id,
            "entryId": "e-same",
            "text": "The market opens at 8.",
            "atMs": int(__import__("time").time() * 1000),
        },
    }
    assert update_event(signal, "agent", b.call_id) is None
    await b._on_native_signal()
    event, is_user = update_event(reply, "agent", b.call_id)
    await b._native_update(event, is_user, track=True)
    await b._native_update(event, is_user, track=True)
    texts = [
        item["async_update"]["payload"][0]["message"]["content"] for item in _updates(b)
    ]
    assert texts == ["The market opens at 8."]
    await b.stop()


@pytest.mark.asyncio
async def test_reply_during_a_stream_gap_is_backfilled_once(monkeypatch):
    b = make(monkeypatch)
    b._desktop_tasks.start("Saturday market")
    await b._on_native_gap()
    await b._on_native_gap()
    assert _updates(b) == []
    event, is_user = __import__(
        "stackchan_mcp.gbot_native_channel", fromlist=["update_event"]
    ).update_event(
        {
            "channel": "box-reply",
            "payload": {
                "agentId": "agent",
                "callId": b.call_id,
                "entryId": "e-gap",
                "text": "The market is on Saturday.",
                "atMs": int(__import__("time").time() * 1000),
            },
        },
        "agent",
        b.call_id,
    )
    await b._native_update(event, is_user, track=True)
    await b._native_update(event, is_user, track=True)
    texts = [
        item["async_update"]["payload"][0]["message"]["content"] for item in _updates(b)
    ]
    assert texts == ["The market is on Saturday."]
    await b.stop()


@pytest.mark.asyncio
async def test_late_reply_after_timeout_is_spoken_once(monkeypatch):
    b = make(monkeypatch)
    b._desktop_tasks.timeout_s = 0.02
    b._response_in_progress = False
    task_id = b._desktop_tasks.start("DPAC lookup")
    await asyncio.sleep(0.08)
    assert task_id not in b._desktop_tasks._open
    await b._native_update(
        _reply("The show is at 7.", "e-late"),
        False,
        track=True,
    )
    await b._native_update(_reply("The show is at 7.", "e-late"), False, track=True)
    texts = [
        item["async_update"]["payload"][0]["message"]["content"] for item in _updates(b)
    ]
    assert texts[-1] == "That DPAC lookup just came back: The show is at 7."
    assert texts.count(texts[-1]) == 1
    await b.stop()


@pytest.mark.asyncio
async def test_cancelled_reply_is_not_spoken_again_and_does_not_start_quiet(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="stackchan_mcp.desktop_tasks")
    caplog.set_level(logging.INFO, logger="stackchan_mcp.gbot_voice_bridge")
    b = make(monkeypatch)
    b._desktop_tasks.quiet_s = 0.05
    task_id = b._desktop_tasks.start("Saturday market")
    await b._native_update(_reply("The market opens at 8.", "e-cancel"), False, track=True)
    await b._handle_event(
        {
            "type": "response.created",
            "response": {"id": "cut", "metadata": {"trigger": "async_update"}},
        }
    )
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._handle_event({"type": "input_audio_buffer.speech_stopped"})
    assert any("outcome=cancelled" in r.message for r in caplog.records)
    assert not any("outcome=delivered" in r.message for r in caplog.records)
    await asyncio.sleep(0.2)
    texts = [
        item["async_update"]["payload"][0]["message"]["content"] for item in _updates(b)
    ]
    assert texts.count("The market opens at 8.") == 1
    assert task_id in b._desktop_tasks._open
    task = b._desktop_tasks._open[task_id]
    assert task.quiet is None
    assert task.pending_playback == 0
    assert not any(f"id={task_id} hop=quiet" in r.message for r in caplog.records)
    await b._native_update(_reply("It opens at nine.", "e-later"), False, track=True)
    texts = [
        item["async_update"]["payload"][0]["message"]["content"] for item in _updates(b)
    ]
    assert texts.count("The market opens at 8.") == 1
    assert texts.count("It opens at nine.") == 1
    assert not any("just came back" in text for text in texts)
    assert task_id in b._desktop_tasks._open
    assert b._desktop_tasks._open[task_id].quiet is None
    await b.stop()


@pytest.mark.asyncio
async def test_warm_does_not_block_the_turn_and_skips_inside_the_window(monkeypatch):
    b = make(monkeypatch)
    b._http = object()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def local(path, payload=None):
        calls.append(path)
        if path == "/native/warm":
            started.set()
            await release.wait()
            return {"ok": True, "state": "ready", "ms": 4}
        raise AssertionError(path)

    b._local = local
    started_at = asyncio.get_running_loop().time()
    await b.send_text("what's the weather this weekend")
    assert asyncio.get_running_loop().time() - started_at < 0.2
    await asyncio.wait_for(started.wait(), 1)
    await b.send_text("again")
    assert calls == ["/native/warm"]
    release.set()
    await b._warm_task
    await b.send_text("later")
    assert calls == ["/native/warm"]
    b._http = None
    await b.stop()


def test_a_fresh_reconnect_still_counts_as_the_channel_up(monkeypatch):
    b = make(monkeypatch)
    b._native.ready = False
    b._native.within_reconnect_grace = lambda: True
    assert b._desktop_channel_ready() is True
    b._native.within_reconnect_grace = lambda: False
    assert b._desktop_channel_ready() is False
