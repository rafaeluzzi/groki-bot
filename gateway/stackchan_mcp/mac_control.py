"""Mac local-control executors for Gemini Live tool calls.

Two lanes, matching how voice conversation tolerates latency:

- Instant lane: open_app / open_url / web_search / media_control / set_volume /
  lock_screen / take_screenshot / run_shortcut / list_shortcuts. Each is a
  short subprocess (open / osascript / pmset / screencapture / shortcuts).
  media_control verifies the actual playback state before returning.
- Task lane: run_mac_task returns immediately with a task id and spawns a
  background `claude -p` process. Completion is pushed back through
  ``on_task_done`` so the bridge can make the robot announce the result.
  check_mac_task lets the model answer "is it done yet?" without waiting.

Safety posture: no raw shell tool is exposed to the model. Arbitrary work
must go through Claude (which applies its own permission model), URLs are
restricted to http/https, and everything else is a fixed verb.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import urllib.parse
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Tool names served by MacController.dispatch(). The bridge routes these to
# the local Mac instead of the ESP32.
MAC_TOOL_NAMES = frozenset(
    {
        "open_app",
        "open_url",
        "web_search",
        "media_control",
        "set_volume",
        "lock_screen",
        "take_screenshot",
        "run_shortcut",
        "list_shortcuts",
        "run_mac_task",
        "check_mac_task",
    }
)

_MEDIA_ACTIONS = {"play", "pause", "play_pause", "next", "previous"}
_MEDIA_NO_PLAYER = "no player running"
_MUSIC_APP = "Music"
_SPOTIFY_APP = "Spotify"
_PLAYING_STATE = "playing"
# Player state lags behind the command: Music can still report the old state
# for 0.1 to 1 s after play/pause. When the state is not the expected one,
# read it again at this pace a few times before deciding.
_MEDIA_STATE_POLL_S = 0.25
_MEDIA_STATE_POLL_TRIES = 6
_NON_PLAYING_STATES = frozenset({"paused", "stopped"})

# AppleScript verbs per player. Spotify and Music share the same vocabulary.
_MEDIA_VERBS = {
    "play": "play",
    "pause": "pause",
    "play_pause": "playpause",
    "next": "next track",
    "previous": "previous track",
}

_CLAUDE_TASK_TIMEOUT_S = 600.0
_CLAUDE_RESULT_MAX_CHARS = 800
_TASK_HISTORY = 10
_MAC_TASK_ESTIMATED_SECONDS = 60

# Voice latency budget: Sonnet at low effort is the fastest configuration
# that still handles real tasks. Shared by ask_claude in the bridge.
# STACKCHAN_CLAUDE_MODEL overrides the model.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"


def claude_model() -> str:
    """Claude model for ask_claude and run_mac_task (STACKCHAN_CLAUDE_MODEL)."""
    return os.getenv("STACKCHAN_CLAUDE_MODEL", "").strip() or DEFAULT_CLAUDE_MODEL


def claude_fast_args() -> tuple[str, ...]:
    """Extra `claude -p` arguments: the configured model at low effort."""
    return ("--model", claude_model(), "--effort", "low")

TaskDoneCallback = Callable[[dict[str, Any]], Awaitable[None]]
TaskStartCallback = Callable[[], Awaitable[None]]


def _claude_bin_name(configured: str | None = None) -> str:
    raw = (configured if configured is not None else os.getenv("STACKCHAN_CLAUDE_BIN") or "claude").strip()
    return raw or "claude"


def find_claude_bin(configured: str | None = None) -> str | None:
    """Return the Claude CLI path, or None when it is not installed."""
    raw = _claude_bin_name(configured)
    path = Path(raw).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return shutil.which(raw)


def resolve_claude_bin(configured: str | None = None) -> str:
    """Resolve the Claude CLI path from an explicit value or the environment.

    Missing binaries are returned unchanged so callers can fail at execution
    time; a warning is emitted at resolve time so gateway start is visible.
    """
    raw = _claude_bin_name(configured)
    found = find_claude_bin(raw)
    if found:
        return found
    logger.warning(
        "claude executable not found (%s); set STACKCHAN_CLAUDE_BIN to an absolute path",
        raw,
    )
    return raw


async def _run_cmd(
    *argv: str,
    timeout: float = 10.0,
    stdin_text: str | None = None,
) -> tuple[int, str, str]:
    """Run argv, return (returncode, stdout, stderr) with a hard timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_text.encode() if stdin_text is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"timed out after {timeout:.0f}s"
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


def _media_player_script() -> str:
    """AppleScript that returns the running supported player name."""
    return (
        f'if application "{_SPOTIFY_APP}" is running then\n'
        f'    return "{_SPOTIFY_APP}"\n'
        f'else if application "{_MUSIC_APP}" is running then\n'
        f'    return "{_MUSIC_APP}"\n'
        f"else\n"
        f'    return "{_MEDIA_NO_PLAYER}"\n'
        f"end if"
    )


def _media_action_script(player: str, action: str) -> str:
    """AppleScript that sends a media action to the selected player."""
    verb = _MEDIA_VERBS[action]
    return f'tell application "{player}" to {verb}'


def _media_script(action: str) -> str:
    """Backward-compatible AppleScript wrapper used by older callers/tests."""
    return (
        f'if application "{_SPOTIFY_APP}" is running then\n'
        f'    {_media_action_script(_SPOTIFY_APP, action)}\n'
        f'    return "{_SPOTIFY_APP}"\n'
        f'else if application "{_MUSIC_APP}" is running then\n'
        f'    {_media_action_script(_MUSIC_APP, action)}\n'
        f'    return "{_MUSIC_APP}"\n'
        f"else\n"
        f'    return "{_MEDIA_NO_PLAYER}"\n'
        f"end if"
    )


def _music_library_shuffle_script() -> str:
    """With an empty Music queue, play does nothing: turn on shuffle and play from the library."""
    return (
        f'tell application "{_MUSIC_APP}"\n'
        "    set shuffle enabled to true\n"
        "    play playlist 1\n"
        "end tell"
    )


def _media_state_script(player: str) -> str:
    """AppleScript that reads the selected player's playback state."""
    if player == _SPOTIFY_APP:
        return f'tell application "{_SPOTIFY_APP}" to get player state as string'
    if player == _MUSIC_APP:
        return f'tell application "{_MUSIC_APP}" to get player state as string'
    return f'return "{_MEDIA_NO_PLAYER}"'



def _chrome_installed() -> bool:
    """Whether Google Chrome is installed in one of the usual places."""
    return any(
        os.path.isdir(p)
        for p in (
            "/Applications/Google Chrome.app",
            os.path.expanduser("~/Applications/Google Chrome.app"),
        )
    )


class MacController:
    """Executes Mac-side tool calls dispatched from the Gemini Live bridge."""

    def __init__(
        self,
        *,
        claude_bin: str | None = None,
        screenshot_dir: Path | None = None,
        on_task_done: TaskDoneCallback | None = None,
        on_task_start: TaskStartCallback | None = None,
        run_cmd: Callable[..., Awaitable[tuple[int, str, str]]] = _run_cmd,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._claude_bin = claude_bin if claude_bin is not None else resolve_claude_bin()
        self._screenshot_dir = screenshot_dir or Path.home() / "Desktop"
        self._on_task_done = on_task_done
        self._on_task_start = on_task_start
        self._run_cmd = run_cmd
        self._sleep = sleep
        self._tasks: deque[dict[str, Any]] = deque(maxlen=_TASK_HISTORY)
        self._task_seq = 0
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._tasks_by_id: dict[int, dict[str, Any]] = {}
        self._task_async_by_id: dict[int, asyncio.Task[None]] = {}

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown mac tool {name}"}
        try:
            return await handler(args)
        except Exception as exc:
            logger.exception("mac tool %s failed", name)
            return {"ok": False, "error": str(exc)}

    # ---- instant lane ------------------------------------------------------

    async def _tool_open_app(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        rc, _, stderr = await self._run_cmd("open", "-a", name)
        if rc != 0:
            return {"ok": False, "error": stderr or f"open -a {name} failed"}
        return {"ok": True, "opened": name}

    async def _tool_open_url(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "only http/https URLs are allowed"}
        rc, _, stderr = await self._run_cmd("open", url)
        if rc != 0:
            return {"ok": False, "error": stderr or "open failed"}
        return {"ok": True, "opened": url}

    async def _tool_web_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        engine = str(args.get("engine", "grok") or "grok").strip().lower()
        if engine not in ("google", "bing", "baidu", "grok"):
            return {"ok": False, "error": "engine must be one of google, bing, baidu, grok"}
        q = urllib.parse.quote_plus(query)
        if engine == "google":
            url = f"https://www.google.com/search?q={q}"
            open_argv = ["open", url]
        elif engine == "bing":
            url = f"https://www.bing.com/search?q={q}"
            open_argv = ["open", url]
        elif engine == "baidu":
            url = f"https://www.baidu.com/s?wd={q}"
            open_argv = ["open", url]
        else:
            # grok: open grok.com in Chrome when installed (uses its signed-in
            # session; ?q= starts a new chat). Without Chrome, use the default
            # browser so `open -a` does not fail.
            url = f"https://grok.com/?q={q}"
            if _chrome_installed():
                open_argv = ["open", "-a", "Google Chrome", url]
            else:
                open_argv = ["open", url]
        logger.info("web_search engine=%s chars=%s", engine, len(query))
        rc, _, stderr = await self._run_cmd(*open_argv)
        if rc != 0:
            return {"ok": False, "error": stderr or "open failed"}
        return {"ok": True, "opened": url, "query": query, "engine": engine}

    async def _tool_media_control(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action", "")).strip()
        if action not in _MEDIA_ACTIONS:
            return {"ok": False, "error": f"action must be one of {sorted(_MEDIA_ACTIONS)}"}
        resolved = await self._resolve_media_player(wait_until_ready=False)
        if isinstance(resolved, dict):
            return resolved
        player, auto_opened = resolved

        pre_state = "stopped"
        if action == "play_pause" and auto_opened is None:
            read_state = await self._read_media_state(player, auto_opened=auto_opened)
            if isinstance(read_state, dict):
                return read_state
            pre_state = read_state
        expected_states = self._expected_media_states(action, pre_state)

        rc, _, stderr = await self._run_cmd(
            "osascript",
            "-e",
            _media_action_script(player, action),
        )
        if rc != 0:
            return self._media_error(
                stderr or "osascript failed",
                auto_opened=auto_opened,
            )

        post_state = await self._await_media_state(
            player, expected_states, auto_opened=auto_opened
        )
        if isinstance(post_state, dict):
            return post_state
        if (
            player == _MUSIC_APP
            and expected_states == frozenset({_PLAYING_STATE})
            and post_state != _PLAYING_STATE
        ):
            rc, _, stderr = await self._run_cmd(
                "osascript",
                "-e",
                _music_library_shuffle_script(),
            )
            if rc != 0:
                return self._media_error(
                    stderr or "osascript failed",
                    auto_opened=auto_opened,
                )
            post_state = await self._await_media_state(
                player, expected_states, auto_opened=auto_opened
            )
            if isinstance(post_state, dict):
                return post_state
        return self._confirm_expected_media_state(
            action,
            player,
            post_state,
            expected_states,
            auto_opened=auto_opened,
        )

    async def _resolve_media_player(
        self,
        *,
        wait_until_ready: bool = True,
    ) -> tuple[str, str | None] | dict[str, Any]:
        rc, stdout, stderr = await self._run_cmd("osascript", "-e", _media_player_script())
        if rc != 0:
            return {"ok": False, "error": stderr or "osascript failed"}
        if stdout != _MEDIA_NO_PLAYER:
            return stdout, None

        open_rc, _, open_stderr = await self._run_cmd("open", "-a", _MUSIC_APP)
        if open_rc != 0:
            return {
                "ok": False,
                "error": open_stderr or "open -a Music failed",
                "auto_opened": _MUSIC_APP,
            }
        if wait_until_ready:
            await self._sleep(3.0)
        return _MUSIC_APP, _MUSIC_APP

    @staticmethod
    def _expected_media_states(action: str, pre_state: str) -> frozenset[str] | None:
        if action == "pause":
            return _NON_PLAYING_STATES
        if action == "play":
            return frozenset({_PLAYING_STATE})
        if action == "play_pause":
            if pre_state == _PLAYING_STATE:
                return _NON_PLAYING_STATES
            return frozenset({_PLAYING_STATE})
        return None

    def _confirm_expected_media_state(
        self,
        action: str,
        player: str,
        state: str,
        expected_states: frozenset[str] | None,
        *,
        auto_opened: str | None = None,
    ) -> dict[str, Any]:
        if expected_states is None or state in expected_states:
            return self._media_success(
                action,
                state,
                player=player,
                auto_opened=auto_opened,
            )
        if expected_states == _NON_PLAYING_STATES and state == _PLAYING_STATE:
            return self._media_error(
                "playback did not stop",
                action=action,
                player=player,
                state=state,
                auto_opened=auto_opened,
            )
        if expected_states == frozenset({_PLAYING_STATE}):
            return self._media_error(
                "playback did not start",
                action=action,
                player=player,
                state=state,
                auto_opened=auto_opened,
            )
        expected = "/".join(sorted(expected_states))
        return self._media_error(
            f"{player} did not reach the expected state: expected {expected}",
            action=action,
            player=player,
            state=state,
            auto_opened=auto_opened,
        )

    async def _await_media_state(
        self,
        player: str,
        expected_states: frozenset[str] | None,
        *,
        auto_opened: str | None = None,
    ) -> str | dict[str, Any]:
        """Read the player state, polling briefly while it is not the expected one,
        so a lagging state is not reported as a failure."""
        state = await self._read_media_state(player, auto_opened=auto_opened)
        tries = 0
        while (
            expected_states is not None
            and not isinstance(state, dict)
            and state not in expected_states
            and tries < _MEDIA_STATE_POLL_TRIES
        ):
            tries += 1
            await self._sleep(_MEDIA_STATE_POLL_S)
            state = await self._read_media_state(player, auto_opened=auto_opened)
        return state

    async def _read_media_state(
        self,
        player: str,
        *,
        auto_opened: str | None = None,
    ) -> str | dict[str, Any]:
        rc, stdout, stderr = await self._run_cmd(
            "osascript",
            "-e",
            _media_state_script(player),
        )
        if rc != 0:
            return self._media_error(
                stderr or f"could not read the playback state of {player}",
                auto_opened=auto_opened,
            )
        return stdout

    @staticmethod
    def _media_success(
        action: str,
        state: str,
        *,
        player: str | None = None,
        auto_opened: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "action": action,
            "player_state": state,
        }
        if player is not None:
            result["player"] = player
        if auto_opened is not None:
            result["auto_opened"] = auto_opened
        return result

    @staticmethod
    def _media_error(
        message: str,
        *,
        action: str | None = None,
        player: str | None = None,
        state: str | None = None,
        auto_opened: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "error": message}
        if action is not None:
            result["action"] = action
        if player is not None:
            result["player"] = player
        if state is not None:
            result["player_state"] = state
        if auto_opened is not None:
            result["auto_opened"] = auto_opened
        return result

    async def _tool_set_volume(self, args: dict[str, Any]) -> dict[str, Any]:
        if "mute" in args and args.get("mute") is not None:
            muted = bool(args["mute"])
            script = f"set volume output muted {'true' if muted else 'false'}"
            rc, _, stderr = await self._run_cmd("osascript", "-e", script)
            if rc != 0:
                return {"ok": False, "error": stderr or "osascript failed"}
            return {"ok": True, "muted": muted}
        level = args.get("level")
        if level is None:
            return {"ok": False, "error": "level or mute is required"}
        level = max(0, min(100, int(level)))
        rc, _, stderr = await self._run_cmd(
            "osascript", "-e", f"set volume output volume {level}"
        )
        if rc != 0:
            return {"ok": False, "error": stderr or "osascript failed"}
        return {"ok": True, "level": level}

    async def _tool_lock_screen(self, args: dict[str, Any]) -> dict[str, Any]:
        # Display sleep locks the Mac when "require password after sleep" is
        # on (macOS default). Avoids the Accessibility permission a synthetic
        # Ctrl-Cmd-Q keystroke would need.
        rc, _, stderr = await self._run_cmd("pmset", "displaysleepnow")
        if rc != 0:
            return {"ok": False, "error": stderr or "pmset failed"}
        return {"ok": True, "locked": True}

    async def _tool_take_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self._screenshot_dir / f"stackchan-{stamp}.png"
        rc, _, stderr = await self._run_cmd("screencapture", "-x", str(path))
        if rc != 0:
            return {"ok": False, "error": stderr or "screencapture failed"}
        return {"ok": True, "path": str(path)}

    async def _tool_run_shortcut(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        rc, stdout, stderr = await self._run_cmd(
            "shortcuts", "run", name, timeout=60.0
        )
        if rc != 0:
            return {"ok": False, "error": stderr or f"shortcut {name} failed"}
        return {"ok": True, "shortcut": name, "output": stdout[:500]}

    async def _tool_list_shortcuts(self, args: dict[str, Any]) -> dict[str, Any]:
        rc, stdout, stderr = await self._run_cmd("shortcuts", "list")
        if rc != 0:
            return {"ok": False, "error": stderr or "shortcuts list failed"}
        names = [line.strip() for line in stdout.splitlines() if line.strip()]
        return {"ok": True, "shortcuts": names[:50]}

    # ---- task lane ---------------------------------------------------------

    async def _tool_run_mac_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task = str(args.get("task", "")).strip()
        if not task:
            return {"ok": False, "error": "task is required"}
        record = self._create_task_record(
            task=task[:200],
            kind="run_mac_task",
            estimated_seconds=_MAC_TASK_ESTIMATED_SECONDS,
            user_message="The task has started in the background.",
            cancelable=False,
        )
        if self._on_task_start is not None:
            try:
                await self._on_task_start()
            except Exception:
                logger.exception("mac task start hook failed")
        self._start_background_task(
            record,
            self._run_claude_task(record, task),
            name=f"mac-task-{record['id']}",
        )
        return self._task_started_response(
            record,
            status="started",
            user_message=(
                "The task has started in the background. A system notice follows when it "
                "finishes; then tell the user the result."
            ),
        )

    async def _tool_check_mac_task(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._tasks:
            return {"ok": True, "tasks": [], "note": "no background tasks have been started"}
        return {"ok": True, "tasks": list(self._tasks)}

    async def cancel_task(self, task_id: int) -> dict[str, Any]:
        """Cancel a background task when Live sends tool_call_cancellation."""
        record = self._tasks_by_id.get(task_id)
        if record is None:
            return {"ok": False, "error": "task not found", "task_id": task_id}
        if not bool(record.get("cancelable")):
            return {
                "ok": False,
                "error": "task is not cancelable",
                "task_id": task_id,
                "state": record.get("state"),
            }
        if record.get("state") != "running":
            return {
                "ok": False,
                "error": "task is no longer running",
                "task_id": task_id,
                "state": record.get("state"),
            }
        task = self._task_async_by_id.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        else:
            self._mark_task_cancelled(record)
        return {"ok": True, "task_id": task_id, "state": record.get("state")}

    def _create_task_record(
        self,
        *,
        task: str,
        kind: str,
        estimated_seconds: int,
        user_message: str,
        cancelable: bool,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._task_seq += 1
        record: dict[str, Any] = {
            "id": self._task_seq,
            "task_id": self._task_seq,
            "task": task,
            "kind": kind,
            "state": "running",
            "status": "running",
            "result": None,
            "estimated_seconds": estimated_seconds,
            "user_message": user_message,
            "cancelable": cancelable,
        }
        if extra:
            record.update(extra)
        self._tasks.append(record)
        self._tasks_by_id[record["id"]] = record
        return record

    def _task_started_response(
        self,
        record: dict[str, Any],
        *,
        status: str,
        user_message: str,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "status": status,
            "state": record["state"],
            "task_id": record["id"],
            "estimated_seconds": record["estimated_seconds"],
            "user_message": user_message,
        }

    def _start_background_task(
        self,
        record: dict[str, Any],
        awaitable: Awaitable[None],
        *,
        name: str,
    ) -> None:
        bg = asyncio.create_task(awaitable, name=name)
        self._bg_tasks.add(bg)
        self._task_async_by_id[record["id"]] = bg
        bg.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task[None]) -> None:
        self._bg_tasks.discard(task)
        task_id: int | None = None
        for current_id, bg in list(self._task_async_by_id.items()):
            if bg is task:
                task_id = current_id
                self._task_async_by_id.pop(current_id, None)
                break
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None or task_id is None:
            return
        record = self._tasks_by_id.get(task_id)
        if record is None or record.get("state") != "running":
            return
        logger.exception("background mac task crashed", exc_info=exc)
        self._finish_task_record(record, {"ok": False, "error": str(exc)})
        if self._on_task_done is None:
            return
        try:
            announce = asyncio.create_task(self._on_task_done(dict(record)))
        except RuntimeError:
            return
        self._bg_tasks.add(announce)
        announce.add_done_callback(self._bg_tasks.discard)

    def _finish_task_record(self, record: dict[str, Any], result: dict[str, Any]) -> None:
        ok = bool(result.get("ok"))
        record["state"] = "done" if ok else "failed"
        record["status"] = record["state"]
        record["result_detail"] = result
        record["result"] = self._task_result_message(record, result)

    @staticmethod
    def _task_result_message(record: dict[str, Any], result: dict[str, Any]) -> str:
        if bool(result.get("ok")):
            return str(result.get("result") or "done")
        return str(result.get("error") or "task failed")

    @staticmethod
    def _mark_task_cancelled(record: dict[str, Any]) -> None:
        record["state"] = "cancelled"
        record["status"] = "cancelled"
        record["result"] = "task cancelled"

    async def _run_claude_task(self, record: dict[str, Any], task: str) -> None:
        try:
            rc, stdout, stderr = await self._run_cmd(
                self._claude_bin,
                "-p",
                task,
                *claude_fast_args(),
                "--permission-mode",
                "acceptEdits",
                timeout=_CLAUDE_TASK_TIMEOUT_S,
            )
            if rc == 0:
                self._finish_task_record(
                    record,
                    {"ok": True, "result": stdout[-_CLAUDE_RESULT_MAX_CHARS:]},
                )
            else:
                self._finish_task_record(
                    record,
                    {"ok": False, "error": (stderr or stdout)[-_CLAUDE_RESULT_MAX_CHARS:]},
                )
        except asyncio.CancelledError:
            self._mark_task_cancelled(record)
            raise
        except Exception as exc:
            logger.exception("mac claude task failed")
            self._finish_task_record(record, {"ok": False, "error": str(exc)})
        if self._on_task_done is not None:
            try:
                await self._on_task_done(dict(record))
            except Exception:
                logger.exception("mac task completion announcement failed")
