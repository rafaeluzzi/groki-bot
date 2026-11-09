# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Experimental realtime audio using a signed-in Grok Bot's ephemeral secret.

Long-lived login credentials remain inside the local login bridge. No secrets,
server error bodies, transcripts, or tool arguments are logged here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import struct
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from .desktop_tasks import DesktopTasks, is_holding_line, log_desktop_hop, log_desktop_task
from .voice_output_queue import VoiceOutputQueue
from .gbot_native_channel import RECONNECT_GRACE_S, WARM_SKIP_S, NativeVoiceChannel

logger = logging.getLogger(__name__)

# Koji's follow-up mic aborts an in-flight desktop reply. Wait this long for a
# real user transcript before saying the cancel was the mic, not the person.
DESKTOP_RESEND_WAIT_S = 2.0


class Pcm16To24:
    """Streaming linear resampler, preserving interpolation across Opus packets."""

    def __init__(self):
        self.samples: list[int] = []
        self.phase = 0

    def convert(self, pcm: bytes) -> bytes:
        self.samples.extend(struct.unpack(f"<{len(pcm) // 2}h", pcm))
        output = []
        while self.phase // 3 + 1 < len(self.samples):
            index, fraction = divmod(self.phase, 3)
            output.append(
                round(
                    (
                        self.samples[index] * (3 - fraction)
                        + self.samples[index + 1] * fraction
                    )
                    / 3
                )
            )
            self.phase += 2
        consumed = self.phase // 3
        self.samples = self.samples[consumed:]
        self.phase -= consumed * 3
        return struct.pack(f"<{len(output)}h", *output)


def validate_bridge_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Grok Bot login bridge must use loopback HTTP")
    return url.rstrip("/")


def validate_voice_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "wss"
        or parsed.hostname != "api.x.ai"
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Grok Bot voice requires wss://api.x.ai")
    return url


def voice_url_with_model(url, model):
    parsed = urlsplit(validate_voice_url(url))
    query = dict(parse_qsl(parsed.query))
    query["model"] = model
    return urlunsplit(parsed._replace(query=urlencode(query)))


async def gather_with_cancel(*awaitables):
    """Run independent requests together and fully cancel siblings on failure."""
    tasks = [asyncio.create_task(awaitable) for awaitable in awaitables]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class GbotVoiceBridge:
    def __init__(
        self,
        *,
        on_audio,
        on_turn_complete,
        on_interrupt,
        on_session_dead,
        bridge_url=None,
        agent_id=None,
        on_transcript=None,
        esp32=None,
        api_key=None,
        response_modality=None,
        on_text=None,
        on_end_conversation=None,
        usb_transport=None,
        on_head_command=None,
        wake_gate_state_getter=None,
        set_face_tracking=None,
        debug_status=None,
        tool_executor=None,
        tool_executor_factory=None,
        tool_extensions=(),
        local_tools=True,
        conversation_idle_timeout_s=8.0,
    ):
        self.url = validate_bridge_url(
            bridge_url
            or os.getenv("STACKCHAN_GBOT_VOICE_URL", "http://127.0.0.1:18771")
        )
        self.agent_id = (
            agent_id
            or os.getenv("STACKCHAN_GBOT_VOICE_BOT_ID")
            or os.getenv("STACKCHAN_TOOL_BOT_ID", "")
        )
        self.language = os.getenv("STACKCHAN_GBOT_VOICE_LANGUAGE", "zh")
        self.call_id = str(uuid.uuid4())
        self._on_transcript = on_transcript
        self._on_text = on_text
        self._on_end_conversation = on_end_conversation
        self._executor = tool_executor
        self._executor_factory = tool_executor_factory
        self._executor_options = dict(
            esp32=esp32,
            usb_transport=usb_transport,
            on_head_command=on_head_command,
            wake_gate_state_getter=wake_gate_state_getter,
            set_face_tracking=set_face_tracking,
            on_end_conversation=on_end_conversation,
            debug_status=debug_status,
            extensions=tool_extensions,
            announce_result=self._announce_result,
            cancel_idle=self._cancel_idle,
        )
        self._local_tools_enabled = local_tools
        self._local_tools = {}
        self._tool_schemas = {}
        self._remote_tools = {}
        self._tool_calls_seen = set()
        self._native = None
        self._desktop_enabled = False
        self._user_epoch = 0
        self._desktop_epoch = 0
        self._user_speaking = False
        self._pending_response = False
        self._response_requested = False
        self._pending_updates = []
        self._idle_task = None
        self._conversation_idle_timeout_s = conversation_idle_timeout_s
        self.audio_bytes_by_response = {}
        self.response_started_at = {}
        self._on_audio = on_audio
        self._on_turn_complete = on_turn_complete
        self._on_interrupt = on_interrupt
        self._on_session_dead = on_session_dead
        self._connected_event = asyncio.Event()
        self._health_ack = asyncio.Event()
        self._health_lock = asyncio.Lock()
        self.response_stall_timeout_s = 30.0
        self._response_progress_at = None
        self._http = None
        self._ws = None
        self._task = None
        self._turn_task = None
        self._output = VoiceOutputQueue()
        self._tools: set[asyncio.Task] = set()
        self._suppressed: set[str] = set()
        self._response_triggers = {}
        self._response_audio_seen = set()
        self._active_response = None
        self._response_in_progress = False
        self._drop_until_response = False
        self._resampler = Pcm16To24()
        self._stopped = False
        self.running = False
        self._desktop_tasks = DesktopTasks(
            announce=self._announce_desktop,
            channel_ready=self._desktop_channel_ready,
            resend=self._resend_desktop_request,
        )
        self._async_sent: list = []
        self._desktop_live: dict = {}
        self._resend_slots: list = []
        self._resend_wait_s = DESKTOP_RESEND_WAIT_S
        self._used_mono = None
        self._warm_task = None
        self.metrics = {
            "input_pcm_bytes": 0,
            "output_pcm_bytes": 0,
            "interruptions": 0,
            "discarded_audio_deltas": 0,
            "speech_starts": 0,
            "server_errors": {},
            "declared_tools": [],
            "tool_events": {},
            "tool_outcomes": {},
            "local_http_status": {},
            "native_outcomes": {},
            "native_updates": {},
            "response_triggers": {},
        }

    def _executor_declares(self, name):
        if self._executor is None:
            return False
        return any(entry.get("name") == name for entry in self._executor.declarations())

    def _bind_portal_core_tool(self, name, used, declarations):
        """Keep a Portal core alias on the local forwarder.

        Returns True when ``name`` is a core spelling. Those calls must not be
        posted to the native desktop harness, even if this session did not
        declare another copy.
        """
        from .core_frames import (
            canonical_core_tool,
            core_action_for_tool,
            core_frames_enabled,
            core_tool_description,
            core_tool_parameters,
        )

        if not core_frames_enabled() or core_action_for_tool(name) is None:
            return False
        if not isinstance(name, str):
            return True
        alias = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
        if not alias or alias in used or name in used:
            return True
        canonical = canonical_core_tool(name)
        if canonical is None or not self._executor_declares(canonical):
            return True
        used.add(alias)
        self._local_tools[alias] = canonical
        self._tool_schemas[alias] = core_tool_parameters(canonical)
        declarations.append(
            {
                "name": alias,
                "type": "function",
                "description": core_tool_description(canonical),
                "parameters": self._tool_schemas[alias],
            }
        )
        return True

    def _bind_portal_panel_tool(self, name, used, declarations):
        """Keep a Portal panel alias on the local forwarder.

        Returns True when ``name`` is a panel spelling. Those calls must not
        be posted to the native desktop harness, even if this session did not
        declare another copy.
        """
        from .panel_frames import (
            canonical_panel_tool,
            panel_action_for_tool,
            panel_frames_enabled,
            panel_tool_description,
            panel_tool_parameters,
        )

        if not panel_frames_enabled() or panel_action_for_tool(name) is None:
            return False
        if not isinstance(name, str):
            return True
        alias = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
        if not alias or alias in used or name in used:
            return True
        canonical = canonical_panel_tool(name)
        if canonical is None or not self._executor_declares(canonical):
            return True
        used.add(alias)
        self._local_tools[alias] = canonical
        self._tool_schemas[alias] = panel_tool_parameters(canonical)
        declarations.append(
            {
                "name": alias,
                "type": "function",
                "description": panel_tool_description(canonical),
                "parameters": self._tool_schemas[alias],
            }
        )
        return True

    def _bind_portal_device_command_tool(self, name, used, declarations):
        """Keep a Portal device-command alias on the local forwarder.

        Returns True when ``name`` is a device_command spelling. Those calls
        must not be posted to the native desktop harness, even if this session
        did not declare another copy.
        """
        from .device_commands import (
            device_command_description,
            device_command_for_tool,
            device_command_parameters,
            device_commands_enabled,
        )

        if not device_commands_enabled() or device_command_for_tool(name) is None:
            return False
        if not isinstance(name, str):
            return True
        alias = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
        if not alias or alias in used or name in used:
            return True
        canonical = device_command_for_tool(name)
        if canonical is None or not self._executor_declares(canonical):
            return True
        used.add(alias)
        self._local_tools[alias] = canonical
        self._tool_schemas[alias] = device_command_parameters(canonical)
        declarations.append(
            {
                "name": alias,
                "type": "function",
                "description": device_command_description(canonical),
                "parameters": self._tool_schemas[alias],
            }
        )
        return True

    def _configure_tools(self, session):
        # Catalog comes from the shared executor; provider-specific wire aliases
        # preserve dotted names without broadening the enabled tool set.
        if self._local_tools_enabled and self._executor is None:
            from .core_frames import core_frame_extension, core_frames_enabled
            from .device_commands import device_command_extension, device_commands_enabled
            from .panel_frames import panel_frame_extension, panel_frames_enabled
            from .voice_tools import VoiceToolExecutor

            options = dict(self._executor_options)
            extensions = tuple(options.get("extensions") or ())
            if core_frames_enabled():
                declared = {
                    entry.get("name")
                    for ext in extensions
                    for entry in getattr(ext, "declarations", ())
                }
                if "core_morph" not in declared:
                    extensions = (*extensions, core_frame_extension(options.get("esp32")))
            if panel_frames_enabled():
                declared = {
                    entry.get("name")
                    for ext in extensions
                    for entry in getattr(ext, "declarations", ())
                }
                if "show_panel" not in declared:
                    extensions = (*extensions, panel_frame_extension(options.get("esp32")))
            if device_commands_enabled():
                declared = {
                    entry.get("name")
                    for ext in extensions
                    for entry in getattr(ext, "declarations", ())
                }
                if "device_command" not in declared:
                    extensions = (*extensions, device_command_extension(options.get("esp32")))
            from .podcast_open import podcast_open_enabled, podcast_open_extension

            if podcast_open_enabled():
                declared = {
                    entry.get("name")
                    for ext in extensions
                    for entry in getattr(ext, "declarations", ())
                }
                if "podcast_open_spotify" not in declared:
                    extensions = (*extensions, podcast_open_extension(options.get("esp32")))
            from .koji_wiki import koji_wiki_enabled, koji_wiki_extension

            if koji_wiki_enabled():
                wiki = koji_wiki_extension()
                if wiki is not None:
                    extensions = (*extensions, wiki)
            options["extensions"] = extensions
            self._executor = (self._executor_factory or VoiceToolExecutor)(**options)
        target = os.getenv("STACKCHAN_TOOL_BOT_ID", "")
        self._desktop_enabled = bool(
            self._local_tools_enabled
            and os.getenv("STACKCHAN_TOOL_BOT", "").strip()
            and target
            and target == self.agent_id
        )
        declarations = []
        self._local_tools.clear()
        self._tool_schemas.clear()
        self._remote_tools.clear()
        used = set()
        if not self._local_tools_enabled:
            return declarations
        native_names = {item.get("name") for item in session.get("tools", [])}
        native_end = (
            "end_the_call" in native_names and self._on_end_conversation is not None
        )
        if self._executor and self._local_tools_enabled:
            for declaration in self._executor.declarations():
                original = declaration["name"]
                if original == "ask_grokbot":
                    continue  # Grok uses its native send_task, never the local Bot alias.
                if original == "end_conversation" and native_end:
                    continue
                alias = re.sub(r"[^a-zA-Z0-9_-]", "_", original)
                if alias in used:
                    raise ValueError("voice tool alias collision")
                used.add(alias)
                self._local_tools[alias] = original
                self._tool_schemas[alias] = declaration.get("parameters", {})
                declarations.append({**declaration, "name": alias, "type": "function"})
        for declaration in session.get("tools", []):
            name = declaration.get("name")
            if self._bind_portal_core_tool(name, used, declarations):
                continue
            if self._bind_portal_panel_tool(name, used, declarations):
                continue
            if self._bind_portal_device_command_tool(name, used, declarations):
                continue
            if name in {"send_task", "search_conversations"}:
                enabled = self._desktop_enabled
            elif name == "end_the_call":
                enabled = self._on_end_conversation is not None
            else:
                enabled = False  # Unknown native tools require an explicit adapter.
            if not enabled:
                continue
            if not isinstance(name, str) or name in used:
                raise ValueError("voice tool name collision")
            used.add(name)
            self._remote_tools[name] = declaration
            self._tool_schemas[name] = declaration.get("parameters", {})
            declarations.append(declaration)
        names = sorted(self._safe_tool_name(item["name"]) for item in declarations)
        self.metrics["declared_tools"] = names
        logger.info("Grok voice declared tools=%s", ",".join(names))
        return declarations

    def _provider_tool_instructions(self):
        if not self._executor or not self._local_tools_enabled:
            return ""
        instructions = self._executor.instructions(model_search=False)
        # Shared local instructions name Gemini's desktop alias. Grok has the
        # native tool and must not be prompted to call an undeclared alias.
        instructions = re.sub(
            r"(?ms)^## Handing tasks to [^\n]+\n\n.*?(?=\n\n|\Z)", "", instructions
        )
        instructions = "\n".join(
            line for line in instructions.splitlines() if "ask_grokbot" not in line
        )
        if "end_the_call" in self._remote_tools:
            instructions = instructions.replace("end_conversation", "end_the_call")
        return instructions + (
            "\nOnly call tools declared in this session. "
            "Use send_task only when it is declared. "
            "If send_task is not declared, or its result says desktop tasks are down, "
            "say desktop tasks are down or answer from what you know. "
            "Do not promise to look it up later or get back to them. "
            "If relay=accepted, a later result or a short failure is spoken even after this turn; do not invent the result. "
            "When that failure says the task did not come back in time, say that. "
            "Say desktop tasks are down only when the tool result says they are down. "
            "When a tool result includes spoken, read only spoken to the user; do not read relay, errorClass, "
            "error codes, field names or debug info."
        )

    def _safe_tool_name(self, name):
        return (
            name
            if (
                isinstance(name, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                and (name in self._local_tools or name in self._remote_tools)
            )
            else "other"
        )

    def _tool_outcome(self, name, outcome):
        # Values originate only from the fixed local branches below, never bodies.
        safe_name = self._safe_tool_name(name)
        key = safe_name + ":" + outcome
        counts = self.metrics["tool_outcomes"]
        counts[key] = counts.get(key, 0) + 1
        logger.info(
            "Grok voice tool outcome name=%s outcome=%s user_epoch=%d desktop_epoch=%d",
            safe_name,
            outcome,
            self._user_epoch,
            self._desktop_epoch,
        )

    @staticmethod
    def _desktop_failure(code, *, refused=False):
        if refused:
            return {"relay": "refused", "spoken": "The desktop assistant did not accept that task."}
        return {
            "relay": "unreachable",
            "spoken": (
                "Desktop tasks are down right now."
                if code == "native_not_ready"
                else "The desktop assistant cannot take that task right now."
            ),
            "errorClass": "voice_bridge",
        }

    def _native_tool_result(self, name, result):
        # VoiceCallHarnessTool.output is protobuf Value -> native JSON. It is
        # neither JSON-encoded text nor an acknowledgement just because HTTP=200.
        raw_kind = result.get("kind") if isinstance(result, dict) else None
        kinds = {
            1: "served",
            2: "unknown_tool",
            3: "invalid_input",
            "VOICE_CALL_HARNESS_TOOL_OUTCOME_KIND_SERVED": "served",
            "VOICE_CALL_HARNESS_TOOL_OUTCOME_KIND_UNKNOWN_TOOL": "unknown_tool",
            "VOICE_CALL_HARNESS_TOOL_OUTCOME_KIND_INVALID_INPUT": "invalid_input",
        }
        kind = (
            raw_kind
            if isinstance(raw_kind, str)
            and raw_kind in {"served", "unknown_tool", "invalid_input"}
            else kinds.get(raw_kind, "other")
            if type(raw_kind) in {int, str}
            else "other"
        )
        output = result.get("output") if isinstance(result, dict) else None
        relay = output.get("relay") if isinstance(output, dict) else None
        relay = (
            relay
            if isinstance(relay, str)
            and relay in {"accepted", "refused", "unreachable"}
            else "invalid"
        )
        key = self._safe_tool_name(name) + ":" + kind + ":" + relay
        counts = self.metrics["native_outcomes"]
        counts[key] = counts.get(key, 0) + 1
        logger.info(
            "Grok native tool result name=%s kind=%s relay=%s",
            self._safe_tool_name(name),
            kind,
            relay,
        )
        if name != "send_task":
            return (
                output
                if kind == "served"
                else {"ok": False, "error": "native tool failed"}
            )
        if kind != "served":
            return self._desktop_failure(kind)
        if relay == "accepted":
            return {"relay": "accepted"}
        if relay == "refused" and isinstance(output.get("spoken"), str):
            return self._desktop_failure("refused", refused=True)
        if (
            relay == "unreachable"
            and isinstance(output.get("spoken"), str)
            and isinstance(output.get("errorClass"), str)
        ):
            return self._desktop_failure("unreachable")
        return self._desktop_failure("invalid_response")

    def _cancel_idle(self):
        if self._idle_task and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
        self._idle_task = None

    async def _finish_turn(self, slot=None):
        await self._on_turn_complete()
        # Playback has drained and tts stop has been sent. A real answer
        # starts the quiet clock only now, after spoken/delivered is logged.
        if isinstance(slot, dict) and slot.get("settled") == "spoken":
            self._desktop_tasks.playback_finished(
                str(slot.get("task_id") or ""),
                holding=is_holding_line(str(slot.get("say") or "")),
            )
        self._cancel_idle()
        if (
            self._on_end_conversation
            and not self._stopped
            and self._conversation_idle_timeout_s > 0
        ):

            async def idle():
                await asyncio.sleep(self._conversation_idle_timeout_s)
                if (
                    not self._stopped
                    and not self._response_in_progress
                    and not self._user_speaking
                ):
                    await self._on_end_conversation()

            self._idle_task = asyncio.create_task(idle())

    async def _flush_pending(self):
        if self._stopped or self._response_in_progress or self._user_speaking:
            if self._pending_updates:
                reason = (
                    "stopped"
                    if self._stopped
                    else "active_response"
                    if self._response_in_progress
                    else "user_speaking"
                )
                logger.info(
                    "Grok native update state=blocked reason=%s pending=%d",
                    reason,
                    len(self._pending_updates),
                )
            return
        if self._response_requested:
            if self._pending_updates:
                logger.info(
                    "Grok native update state=blocked reason=manual_requested pending=%d",
                    len(self._pending_updates),
                )
            return
        if self._pending_response:
            self._pending_response = False
            self._response_requested = True
            self._response_progress_at = time.monotonic()
            await self._send({"type": "response.create"})
            return
        while self._pending_updates:
            event = self._pending_updates.pop(0)
            slot = event.pop("_desktop_slot", None) if isinstance(event, dict) else None
            # Client-native orchestrated updates do not reserve a manual
            # response slot: the server may choose not to create a response.
            await self._send(event)
            if isinstance(event, dict) and event.get("type") == "async_update.add":
                self._async_sent.append(slot)
            topic = event.get("async_update", {}).get("topic")
            topic = topic if topic in {"work_landed", "user_message"} else "other"
            counts = self.metrics["native_updates"]
            key = topic + ":sent"
            counts[key] = counts.get(key, 0) + 1
            logger.info(
                "Grok native update topic=%s state=sent pending=%d",
                topic,
                len(self._pending_updates),
            )

    async def _request_response(self):
        self._pending_response = True
        await self._flush_pending()

    def _desktop_channel_ready(self) -> bool:
        """Up when the stream is ready, or it dropped a moment ago and is reconnecting."""
        native = self._native
        if native is None or not self.running or self._stopped:
            return False
        if getattr(native, "ready", False):
            return True
        grace = getattr(native, "within_reconnect_grace", None)
        return bool(callable(grace) and grace())

    async def _announce_desktop(self, text: str) -> bool:
        if self._stopped or not self.running:
            return False
        return await self._announce_result(text)

    def take_desktop_work(self) -> list:
        return self._desktop_tasks.export_for_handoff()

    async def say_desktop_later(self, item: dict) -> bool:
        """Speak a result the previous voice session could not, or keep its timer."""
        if item.get("open"):
            self._desktop_tasks.adopt(item)
            return True
        if item.get("remember"):
            self._desktop_tasks.remember(item)
            return True
        if self._stopped or not self.running:
            return False
        if item.get("reply"):
            slot = {
                "task_id": str(item.get("id") or ""),
                "started": str(item.get("start") or ""),
                "waited_ms": int(item.get("waited_ms") or 0),
                "say": item.get("text") or "",
                "resent": False,
                "settled": False,
            }
            return await self._announce_result(item.get("text") or "", slot=slot)
        said = await self._announce_desktop(item.get("text") or "")
        if said:
            log_desktop_task(
                str(item["id"]),
                str(item["start"]),
                str(item["outcome"]),
                waited_ms=int(item.get("waited_ms") or 0),
            )
        return said

    def _maybe_warm(self) -> None:
        """Wake a desktop that has been idle. The voice turn does not wait."""
        if self._http is None or not self._desktop_enabled:
            return
        if self._used_mono is not None and (time.monotonic() - self._used_mono) < WARM_SKIP_S:
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        self._warm_task = asyncio.create_task(self._warm_desktop(), name="desktop-warm")

    async def _warm_desktop(self) -> None:
        logger.info("desktop warm hop=warm_start")
        started = time.monotonic()
        try:
            result = await self._local("/native/warm", {})
        except Exception:
            logger.info("desktop warm hop=warm_ready ms=%d state=failed", int((time.monotonic() - started) * 1000))
            return
        ms = result.get("ms") if isinstance(result, dict) else None
        if not isinstance(ms, int):
            ms = int((time.monotonic() - started) * 1000)
        state = result.get("state") if isinstance(result, dict) else None
        state = state if state in {"ready", "in_flight", "failed"} else "ready"
        logger.info("desktop warm hop=warm_ready ms=%d state=%s", ms, state)
        if state == "ready":
            self._used_mono = time.monotonic()

    async def _on_native_gap(self) -> None:
        """The box stream dropped. The transcript backfill speaks anything it missed."""
        task = self._desktop_tasks.oldest()
        if task is not None:
            log_desktop_hop(task.id, "stream_gap")

    async def _on_native_signal(self) -> None:
        """A nudge arrived. It is not spoken; the transcript entry is the result."""
        task = self._desktop_tasks.oldest()
        if task is not None:
            log_desktop_hop(task.id, "signal")

    def _warm_in_flight(self) -> bool:
        return self._warm_task is not None and not self._warm_task.done()

    async def _wait_for_native(self) -> tuple[int, bool]:
        """Wait out a short reconnect. Returns (ms, try_anyway).

        try_anyway is true when the box may still accept a task while the event
        stream is down: a reconnect blip, or a desktop that is waking. The 16:52
        Cary task was accepted in 350ms during a 20s warm. A long outage
        returns at once and try_anyway is false.
        """
        native = self._native
        if native is None:
            return 0, False
        if native.ready:
            return 0, False
        warming = self._warm_in_flight()
        grace = getattr(native, "within_reconnect_grace", None)
        recent = bool(callable(grace) and grace())
        if not recent and not warming:
            return 0, False
        if not recent:
            # Waking, stream never looked up. Send now; the box accepted Cary that way.
            return 0, True
        started = time.monotonic()
        deadline = started + RECONNECT_GRACE_S
        while time.monotonic() < deadline:
            if native.ready or self._stopped:
                break
            await asyncio.sleep(0.05)
        return int((time.monotonic() - started) * 1000), True

    async def _resend_desktop_request(self, request: str) -> bool:
        """Same request, once, on the open task. Does not open a second tracker."""
        if self._stopped or not self.running:
            return False
        try:
            result = await self._local(
                "/tool",
                {
                    "agentId": self.agent_id,
                    "callId": self.call_id,
                    "name": "send_task",
                    "input": {"request": request},
                    "line": {
                        "theyJustTalked": True,
                        "newsOwed": False,
                        "receiptOwed": False,
                        "alreadySpokeThisTurn": False,
                    },
                },
            )
        except Exception:
            logger.warning("desktop task resend request failed")
            return False
        parsed = self._native_tool_result("send_task", result)
        return parsed.get("relay") == "accepted"

    async def _native_from_channel(self, event, is_user):
        await self._native_update(event, is_user, track=True)

    def _note_user_followed(self) -> None:
        for slot in self._resend_slots:
            slot["user_followed"] = True

    def _schedule_resend(self, slot: dict) -> None:
        # TODO(koji): Koji's follow-up mic aborts an in-flight desktop result
        # (Portal interrupt → response.cancel) before the answer is heard.
        # Stop opening that mic over an async_update. This one re-send is a stopgap.
        waiter = asyncio.create_task(self._resend_after(slot), name="desktop-resend")
        slot["resend_task"] = waiter
        self._resend_slots.append(slot)

    async def _resend_after(self, slot: dict) -> None:
        try:
            await asyncio.sleep(self._resend_wait_s)
        except asyncio.CancelledError:
            return
        if slot.get("user_followed") or slot.get("resent") or self._stopped or not self.running:
            return
        slot["resent"] = True
        await self._announce_result(
            slot["say"],
            slot={
                "task_id": slot["task_id"],
                "started": slot["started"],
                "waited_ms": slot["waited_ms"],
                "say": slot["say"],
                "resent": True,
                "settled": False,
            },
        )

    def _settle_spoken(self, slot: dict) -> None:
        if slot.get("settled"):
            return
        slot["settled"] = "spoken"
        self._desktop_tasks.note_outcome(str(slot.get("task_id") or ""))
        log_desktop_hop(slot["task_id"], "spoken")
        log_desktop_task(
            slot["task_id"], slot["started"], "delivered", waited_ms=slot["waited_ms"]
        )

    def _settle_cancelled(self, slot: dict) -> None:
        if slot.get("settled"):
            return
        slot["settled"] = "cancelled"
        self._desktop_tasks.note_outcome(str(slot.get("task_id") or ""))
        log_desktop_hop(slot["task_id"], "cancelled")
        log_desktop_task(
            slot["task_id"], slot["started"], "cancelled", waited_ms=slot["waited_ms"]
        )
        if not slot.get("resent"):
            self._schedule_resend(slot)

    def _settle_desktop_response(self, response_id, event):
        slot = self._desktop_live.pop(response_id, None)
        if slot is None or slot.get("settled"):
            return None
        status = (event.get("response") or {}).get("status") if isinstance(event, dict) else None
        cancelled = response_id in self._suppressed or status in {"cancelled", "canceled"}
        audio = self.audio_bytes_by_response.get(response_id, 0) > 0
        if cancelled:
            self._settle_cancelled(slot)
        elif audio and status not in {"failed"}:
            self._settle_spoken(slot)
        return slot

    async def _native_update(self, event, is_user, *, track=False):
        topic = event.get("async_update", {}).get("topic")
        topic = topic if topic in {"work_landed", "user_message"} else "other"
        state = "dropped" if self._stopped or not self.running else "queued"
        counts = self.metrics["native_updates"]
        key = topic + ":" + state
        counts[key] = counts.get(key, 0) + 1
        logger.info("Grok native update topic=%s state=%s", topic, state)
        if track and topic == "work_landed" and not is_user:
            entry_id = event.get("desktop_entry") or event.get("event_id") or ""
            if not isinstance(entry_id, str) or not entry_id:
                entry_id = "anon-" + uuid.uuid4().hex
            text = ""
            at_ms = int(time.time() * 1000)
            payload = event.get("async_update", {}).get("payload") or []
            if payload and isinstance(payload[0], dict):
                content = (payload[0].get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content
                stamp = payload[0].get("timestampMs")
                if isinstance(stamp, (int, float)):
                    at_ms = int(stamp)
            if entry_id in self._desktop_tasks._seen:
                logger.info("Grok native update topic=%s state=ignored reason=duplicate", topic)
                return
            decision = self._desktop_tasks.accept_reply(entry_id, text, at_ms)
            if decision is None:
                logger.info("Grok native update topic=%s state=ignored reason=no_task", topic)
                return
            say = decision["say"]
            event = {
                "type": "async_update.add",
                "event_id": event.get("event_id") or entry_id,
                "async_update": {
                    "topic": "work_landed",
                    "payload": [
                        {
                            "message": {"type": "text", "content": say},
                            "timestampMs": at_ms,
                        }
                    ],
                },
                "_desktop_slot": {
                    "task_id": decision["task_id"],
                    "started": decision["started"],
                    "waited_ms": decision["waited_ms"],
                    "say": say,
                    "resent": False,
                    "settled": False,
                },
            }
        if self._stopped or not self.running:
            slot = event.get("_desktop_slot") if isinstance(event, dict) else None
            if isinstance(slot, dict):
                self._desktop_tasks.stash(slot)
            return
        if is_user:
            self._note_user_followed()
            self._user_epoch += 1
            self._cancel_idle()
            if self._executor:
                text = event["async_update"]["payload"][0]["message"]["content"]
                await self._executor.observe_user_input(text, finished=True)
        self._pending_updates.append(event)
        await self._flush_pending()

    async def _announce_result(self, text, slot=None):
        # Shared executor already labels this as untrusted result data.
        if self._stopped:
            return False
        event = {
            "type": "async_update.add",
            "event_id": "local-result-" + str(uuid.uuid4()),
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
        if slot is not None:
            event["_desktop_slot"] = slot
        await self._native_update(event, False)
        return True

    @property
    def current_turn_emotion_face(self):
        return getattr(self._executor, "current_turn_emotion_face", None)

    def clear_turn_emotion_face(self):
        if self._executor:
            self._executor.clear_turn_emotion_face()

    async def send_keepalive_audio(self, pcm):
        await self._send_pcm(pcm)

    async def send_activity_start(self):
        self._cancel_idle()

    async def send_activity_end(self):
        await self.send_audio_stream_end()

    async def clear_conversation_context(self):
        # A new call owns fresh context; the realtime API has no clear-all verb.
        self._pending_response = False
        self._cancel_idle()

    async def inject_user_text(self, text):
        # Programmatic speech/notices do not grant desktop dispatch authority.
        if self._stopped:
            return False
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._request_response()
        return True

    async def _local(self, path, payload=None):
        assert self._http is not None
        method = "GET" if payload is None else "POST"
        async with self._http.request(
            method, self.url + path, json=payload, allow_redirects=False
        ) as response:
            route = path if path in {"/credential", "/session", "/tool"} else "other"
            key = route + ":" + str(response.status)
            counts = self.metrics["local_http_status"]
            counts[key] = counts.get(key, 0) + 1
            logger.info("Grok local request route=%s status=%d", route, response.status)
            if response.status != 200:
                try:
                    reason = (await response.json()).get("error")
                except Exception:
                    reason = None
                safe_reasons = {
                    "login_unavailable", "login_expired_open_grok_bot", "unsupported_storage",
                    "upstream_http_401", "upstream_http_403", "upstream_http_404",
                    "upstream_http_429", "bridge_request_failed",
                }
                self.metrics["connection_error"] = (
                    reason if isinstance(reason, str) and reason in safe_reasons
                    else "connection_failed"
                )
                raise RuntimeError("Grok Bot local bridge request failed")
            result = await response.json()
        if result.get("ok") is False:
            raise RuntimeError("Grok Bot local bridge rejected request")
        return result

    async def start(self):
        stage = "account_requests"
        self.metrics["startup_stage"] = stage
        trace = aiohttp.TraceConfig()

        async def reject_redirect(*_args):
            raise RuntimeError("Voice redirects are disabled")

        trace.on_request_redirect.append(reject_redirect)
        self._http = aiohttp.ClientSession(
            trust_env=False,
            trace_configs=[trace],
            timeout=aiohttp.ClientTimeout(total=30),
        )
        try:
            # Independent requests; keep failure cleanup compatible with Python 3.10.
            minted, session = await gather_with_cancel(
                self._local("/credential"),
                self._local(
                    "/session",
                    {
                        "agentId": self.agent_id,
                        "agentName": "Groki",
                        "spokenLanguage": self.language,
                    },
                ),
            )
            stage = "voice_websocket"
            self.metrics["startup_stage"] = stage
            credential = minted["credential"]
            url = voice_url_with_model(credential["websocketUrl"], credential["model"])
            if float(credential["expiresAtUnixSeconds"]) <= time.time() + 5:
                raise RuntimeError("Grok Bot temporary credential expired")
            self._ws = await self._http.ws_connect(
                url,
                protocols=["xai-client-secret." + credential["clientSecret"]],
                heartbeat=20,
                max_msg_size=8 * 1024 * 1024,
            )
            del credential
            stage = "configure_tools"
            self.metrics["startup_stage"] = stage
            declarations = self._configure_tools(session)
            stage = "provider_instructions"
            self.metrics["startup_stage"] = stage
            config = {
                "instructions": session.get("instructions", "Always speak English. Keep replies short and natural.")
                + (
                    "\n\n" + self._provider_tool_instructions()
                    if self._executor and self._local_tools_enabled
                    else ""
                ),
                "resumption": {"enabled": True},
                "turn_detection": {"type": "server_vad"},
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {
                            "model": "grok-transcribe",
                            "language_hint": self.language,
                        },
                    },
                    "output": {"format": {"type": "audio/pcm", "rate": 24000}},
                },
                "tools": declarations,
            }
            if self.language == "auto":
                config["audio"]["input"]["transcription"].pop("language_hint")
            if os.getenv("STACKCHAN_GBOT_VOICE_NAME"):
                config["voice"] = os.environ["STACKCHAN_GBOT_VOICE_NAME"]
            stage = "session_update"
            self.metrics["startup_stage"] = stage
            await self._send({"type": "session.update", "session": config})
            self.running = True
            self._task = asyncio.create_task(self._receive())
            if self._desktop_enabled:
                stage = "native_presence"
                self.metrics["startup_stage"] = stage
                logger.info("desktop call start id=%s", self.call_id)
                self._native = NativeVoiceChannel(
                    self._http,
                    self.url,
                    self.agent_id,
                    self.call_id,
                    self._native_from_channel,
                    on_gap=self._on_native_gap,
                    on_signal=self._on_native_signal,
                )
                await self._native.start()
            self.metrics["startup_stage"] = "started"
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception as exc:
            error_type = type(exc).__name__
            if error_type not in {
                "TypeError",
                "AttributeError",
                "ValueError",
                "RuntimeError",
                "KeyError",
                "ClientResponseError",
                "WSServerHandshakeError",
                "TimeoutError",
                "ClientConnectorError",
            }:
                error_type = "other"
            self.metrics["startup_error_type"] = error_type
            logger.warning(
                "Grok voice startup failed stage=%s error_type=%s", stage, error_type
            )
            await self.stop()
            raise RuntimeError(
                "Grok Bot voice startup failed (details redacted)"
            ) from None

    async def wait_connected(self, timeout=10.0):
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self.running

    async def check_connection(self, timeout=10.0):
        """Require a new acknowledgement on the actual voice WebSocket."""
        async with self._health_lock:
            if self._stopped or not self.running or not self._connected_event.is_set():
                return False
            if (
                self._response_progress_at is not None
                and not self._user_speaking
                and not self._tools
                and time.monotonic() - self._response_progress_at > self.response_stall_timeout_s
            ):
                self.metrics["connection_error"] = "response_stalled"
                return False
            self._health_ack.clear()

            async def probe():
                await self._send({"type": "session.update", "session": {}})
                await self._health_ack.wait()

            try:
                await asyncio.wait_for(probe(), timeout)
            except Exception:
                return False
            return self.running and not self._stopped

    async def _send(self, event):
        if self._ws is None or self._ws.closed:
            raise RuntimeError("Grok Bot voice is disconnected")
        await self._ws.send_json(event)

    async def send_audio(self, pcm):
        self._cancel_idle()
        await self._send_pcm(pcm)

    async def _send_pcm(self, pcm):
        audio = self._resampler.convert(pcm)
        if audio:
            await self._send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio).decode("ascii"),
                }
            )
            self.metrics["input_pcm_bytes"] += len(audio)

    async def send_audio_stream_end(self):
        # Server VAD commits completed speech. Explicit commits of empty buffers fail.
        return None

    async def send_text(self, text):
        self._maybe_warm()
        self._cancel_idle()
        self._user_epoch += 1
        if self._executor:
            await self._executor.observe_user_input(text, finished=True)
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._request_response()

    async def interrupt(self):
        self._cancel_idle()
        self._response_progress_at = None
        self._pending_response = False
        active = self._response_in_progress or bool(
            self._turn_task and not self._turn_task.done()
        )
        self._drop_until_response = True
        if self._active_response:
            self._suppressed.add(self._active_response)
            slot = self._desktop_live.pop(self._active_response, None)
            if slot is not None:
                self._settle_cancelled(slot)
        await self._output.cancel()
        self._turn_task = None
        await self._on_interrupt()
        if active:
            self.metrics["interruptions"] += 1
        if self._response_in_progress:
            self._response_in_progress = False
            self._response_requested = False
            await self._send({"type": "response.cancel"})

    async def _handle_event(self, event):
        kind = event.get("type")
        if kind == "session.updated":
            self._connected_event.set()
            self._health_ack.set()
        elif kind == "response.created":
            self._response_progress_at = time.monotonic()
            self._active_response = event.get("response", {}).get("id")
            self._response_in_progress = True
            self._response_requested = False
            metadata = event.get("response", {}).get("metadata") or {}
            trigger = metadata.get("trigger") if isinstance(metadata, dict) else None
            trigger = (
                trigger
                if trigger in {"async_update", "server_vad", "response_create"}
                else "other"
            )
            # The box reply is the update we just sent. The server often labels
            # that response trigger=other (13e84dcebc96), so the slot never
            # settled and the answer never logged spoken. A user barge-in
            # (server_vad) is not that reply.
            if self._async_sent and self._active_response and trigger != "server_vad":
                pending = self._async_sent.pop(0)
                if pending is not None:
                    pending["response_id"] = self._active_response
                    self._desktop_live[self._active_response] = pending
            self._response_triggers[self._active_response] = trigger
            counts = self.metrics["response_triggers"]
            counts[trigger] = counts.get(trigger, 0) + 1
            logger.info("Grok voice response state=created trigger=%s", trigger)
            self.response_started_at[self._active_response] = time.monotonic()
            self._drop_until_response = False
        elif kind == "input_audio_buffer.speech_started":
            self._maybe_warm()
            self._response_progress_at = None
            self.metrics["speech_starts"] += 1
            self._user_epoch += 1
            self._user_speaking = True
            self._cancel_idle()
            if self._response_in_progress or (
                self._turn_task and not self._turn_task.done()
            ):
                await self.interrupt()
        elif kind == "input_audio_buffer.speech_stopped":
            self._response_progress_at = time.monotonic()
            self._user_speaking = False
            # Native completions must not depend on a subsequent VAD response.
            # Leave ordinary response.create requests to their normal path.
            if self._pending_updates:
                await self._flush_pending()
        elif kind in {"response.audio.delta", "response.output_audio.delta"}:
            response_id = event.get("response_id") or self._active_response
            if self._drop_until_response or response_id in self._suppressed:
                self.metrics["discarded_audio_deltas"] += 1
                return
            self._response_progress_at = time.monotonic()
            audio = base64.b64decode(event.get("delta", ""), validate=True)
            if response_id not in self._response_audio_seen:
                self._response_audio_seen.add(response_id)
                logger.info(
                    "Grok voice response state=first_audio trigger=%s bytes=%d",
                    self._response_triggers.get(response_id, "other"),
                    len(audio),
                )
            self.metrics["output_pcm_bytes"] += len(audio)
            self.audio_bytes_by_response[response_id] = (
                self.audio_bytes_by_response.get(response_id, 0) + len(audio)
            )
            self._output.submit(lambda: self._on_audio(audio))
            await asyncio.sleep(0)
        elif kind in {
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
            "conversation.item.input_audio_transcription.completed",
        }:
            if self._on_transcript:
                await self._on_transcript(event)
            text = event.get("transcript", "")
            if kind == "conversation.item.input_audio_transcription.completed":
                if isinstance(text, str) and text.strip():
                    self._note_user_followed()
                if self._executor:
                    await self._executor.observe_user_input(text, finished=True)
            elif self._on_text and text:
                await self._on_text(text)
        elif kind == "response.done":
            response_id = event.get("response", {}).get("id") or self._active_response
            if response_id != self._active_response:
                return
            self._response_progress_at = None
            self._response_in_progress = False
            slot = self._settle_desktop_response(response_id, event)
            if response_id not in self._suppressed:
                # Order normal completion after its audio without blocking VAD.
                # Quiet starts when that playback finishes, not when the reply was queued.
                self._turn_task = self._output.submit(lambda slot=slot: self._finish_turn(slot))
                await asyncio.sleep(0)
            await self._flush_pending()
        elif kind == "response.function_call_arguments.done":
            name = self._safe_tool_name(event.get("name"))
            has_id = isinstance(event.get("call_id"), str) and bool(event["call_id"])
            counts = self.metrics["tool_events"]
            counts[name] = counts.get(name, 0) + 1
            logger.info(
                "Grok voice tool event name=%s call_id_present=%s user_epoch=%d desktop_epoch=%d",
                name,
                has_id,
                self._user_epoch,
                self._desktop_epoch,
            )
            raw_name = event.get("name")
            if isinstance(raw_name, str) and (
                raw_name in self._local_tools or raw_name in self._remote_tools
            ):
                task = asyncio.create_task(self._tool(event))
                self._tools.add(task)
                task.add_done_callback(self._tools.discard)
            else:
                self._tool_outcome(name, "undeclared")
        elif kind == "error":
            error = event.get("error", {})
            code = error.get("code")
            param = error.get("param")
            allowed_codes = {
                "response_cancel_not_active",
                "invalid_request_error",
                "invalid_value",
                "rate_limit_exceeded",
                "server_error",
                "authentication_error",
                "session_expired",
            }
            allowed_params = {
                "response",
                "session",
                "audio",
                "session.voice",
                "session.audio",
                "model",
                "tools",
            }
            code = code if code in allowed_codes else "other"
            param = param if param in allowed_params else "other"
            key = code + ":" + param
            errors = self.metrics["server_errors"]
            errors[key] = errors.get(key, 0) + 1
            logger.warning("Grok Bot voice server error code=%s param=%s", code, param)

    async def _tool(self, event):
        call_id = event.get("call_id")
        name = event.get("name")
        if not isinstance(call_id, str) or not call_id:
            self._tool_outcome(name, "missing_call_id")
            return
        if call_id in self._tool_calls_seen:
            self._tool_outcome(name, "duplicate_call_id")
            return
        if len(self._tool_calls_seen) >= 4096:
            self._tool_outcome(name, "call_limit")
            return
        self._tool_calls_seen.add(call_id)
        outcome = "returned"
        original = self._local_tools.get(name)
        try:
            try:
                arguments = json.loads(event.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                outcome = "invalid_arguments"
                raise
            if not isinstance(arguments, dict):
                outcome = "invalid_arguments"
                raise ValueError("invalid tool arguments")
            from .voice_tools import _valid_arguments

            if not _valid_arguments(arguments, self._tool_schemas.get(name, {})):
                outcome = "invalid_arguments"
                raise ValueError("invalid tool arguments")
            waited_ms = 0
            try_anyway = False
            if name == "send_task" and not (self._native and self._native.ready):
                waited_ms, try_anyway = await self._wait_for_native()
            stream_down = not (self._native and self._native.ready)
            blocked = name in {"send_task", "search_conversations"} and stream_down and not (
                name == "send_task" and try_anyway
            )
            if blocked:
                outcome = "native_not_ready"
                if name == "send_task":
                    self._desktop_tasks.refuse(waited_ms)
                    result = self._desktop_failure("native_not_ready")
                else:
                    result = {
                        "ok": False,
                        "error": "Desktop tasks are down right now.",
                        "spoken": "Desktop tasks are down right now.",
                    }
            elif name == "send_task":
                request = arguments.get("request")
                if (
                    not self._desktop_enabled
                    or self._user_epoch <= self._desktop_epoch
                    or not isinstance(request, str)
                    or not request.strip()
                    or len(request) > 20000
                ):
                    outcome = (
                        "desktop_disabled"
                        if not self._desktop_enabled
                        else "new_user_required"
                        if self._user_epoch <= self._desktop_epoch
                        else "invalid_arguments"
                    )
                    result = self._desktop_failure(outcome, refused=True)
                else:
                    self._desktop_epoch = self._user_epoch
                    result = await self._local(
                        "/tool",
                        {
                            "agentId": self.agent_id,
                            "callId": self.call_id,
                            "name": "send_task",
                            "input": {"request": request},
                            "line": {
                                "theyJustTalked": True,
                                "newsOwed": False,
                                "receiptOwed": False,
                                "alreadySpokeThisTurn": False,
                            },
                        },
                    )
                    result = self._native_tool_result("send_task", result)
                    if result.get("relay") == "accepted":
                        outcome = "native_accepted"
                        self._used_mono = time.monotonic()
                        self._desktop_tasks.start(request, waited_ms=waited_ms)
                    elif stream_down:
                        outcome = "native_not_ready"
                        self._desktop_tasks.refuse(waited_ms)
                        result = self._desktop_failure("native_not_ready")
                    else:
                        outcome = "native_" + result["relay"]
            elif name == "end_the_call":
                result = {"ok": True}
                if self._on_end_conversation:
                    await self._on_end_conversation()
            elif original and self._executor:
                result = await self._executor.execute(original, arguments)
            elif name in self._remote_tools:
                result = await self._local(
                    "/tool",
                    {
                        "agentId": self.agent_id,
                        "callId": self.call_id,
                        "name": name,
                        "input": arguments,
                        "line": {
                            "theyJustTalked": True,
                            "newsOwed": False,
                            "receiptOwed": False,
                            "alreadySpokeThisTurn": False,
                        },
                    },
                )
                result = self._native_tool_result(name, result)
            else:
                result = {"ok": False, "error": "voice tool not enabled"}
        except asyncio.CancelledError:
            raise
        except Exception:
            if outcome == "returned":
                outcome = "failed"
            result = (
                self._desktop_failure(outcome)
                if name == "send_task"
                else {"ok": False, "error": "voice tool failed"}
            )
        self._tool_outcome(name, outcome)
        if self._stopped or not self.running:
            return
        try:
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    },
                }
            )
            await self._request_response()
        except Exception:
            logger.warning("Grok Bot voice tool response failed (details redacted)")

    async def _receive(self):
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_event(json.loads(message.data))
                elif message.type == aiohttp.WSMsgType.ERROR:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Grok Bot voice receive failed (details redacted)")
        finally:
            # A sink callback can initiate stop() and await this receiver. Its
            # owning stop() already cancelled output, so do not await it back.
            if not self._stopped:
                await self._output.cancel()
            self.running = False
            self._connected_event.clear()
            if not self._stopped:
                if self._native:
                    await self._native.close()
                if self._executor:
                    await self._executor.close()
                self._pending_updates.clear()
                for task in self._tools:
                    task.cancel()
                await self._on_session_dead()

    async def stop(self):
        self._stopped = True
        resends = [
            slot.get("resend_task")
            for slot in self._resend_slots
            if slot.get("resend_task") is not None and not slot["resend_task"].done()
        ]
        for task in resends:
            task.cancel()
        if resends:
            await asyncio.gather(*resends, return_exceptions=True)
        await self._desktop_tasks.aclose()
        self._cancel_idle()
        self._pending_updates.clear()
        self._pending_response = False
        self.running = False
        self._connected_event.clear()
        await self._output.cancel()
        if self._native:
            await self._native.close()
        if self._executor:
            await self._executor.close()
        current = asyncio.current_task()
        tasks = [t for t in [self._task, *self._tools] if t and t is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._ws:
            await self._ws.close()
        if self._http:
            await self._http.close()
