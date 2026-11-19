# SPDX-License-Identifier: MIT
"""One Portal skill block. It replaces the scattered one-voice instruction pieces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CONTRACT = Path(__file__).resolve().parents[1] / "contract" / "koji-contract.json"

_RULES = (
    "You are the only voice. The Portal does not speak. "
    "Call the tool before speaking, and speak one short line after the result. "
    "confirmed:false means not sure it landed. Never retry silently. "
    "Portal first. Use a Mac tool only when the user names the Mac. "
    "Never invent state. Use the read tools. "
    "koji_context.reflex tells you what the Portal already did as a reflex "
    "(for example ducked, launch_cancelled), so do not repeat it.\n"
    "Before you answer, decide whether the request needs a Portal action. "
    "Questions about the world never call a Portal tool. "
    "Questions about the Portal's state use the latest portal_state, or call get_portal_state.\n"
    "If an action is needed, call the tool first. After the result, say one natural sentence "
    "that carries the action and your reply. Do not say 'done', 'command sent' or tool names. "
    "Do not confirm twice. The Portal's tick already says it landed.\n"
    "If koji_context.reflex shows the Portal already ducked or cancelled something, do not repeat it. "
    "Decide the next step.\n"
    "If ok is false or confirmed is false, say plainly what happened in one line and offer one alternative.\n"
    "Never state a Portal fact that is not in the latest portal_state.\n"
    "portal_state.panel tells you whether a panel is open, who opened it, whether it is waiting "
    "(for a pick, a Spotify start, a list), and the last panel that closed and why.\n"
    "If an action is in_progress, do not issue it again; say it's on its way.\n"
    "If portal_state is stale, say so before relying on it.\n"
    "Before you list followed shows or build a followed-shows panel, call podcast_followed "
    "and use only its result, even if the user names specific shows.\n"
    "When the user says a number, pass ordinal. Do not turn it into a title. "
    "Call podcast_open_spotify again only once, and only when did_you_mean is set. "
    "Otherwise tell the user it isn't followed and offer to follow it. "
    "Never use podcast_open_spotify to follow. "
    "One open per turn. If the result says one_open_per_turn, say the show that already opened. "
    "One podcast_follow per turn. If the result says already_handled, do not call it again. "
    "Say one short line from say after the result.\n"
    "A panel document is version 1. Kinds are list, now_playing, choice, and webview. "
    "Webview templates are weather, compare, agenda, steps, fact, score, key_points, and chart. "
    "A list titled Podcasts I follow is A-Z, numbered from the top of titled rows.\n"
)

_EXAMPLES = (
    'User: "Put on the latest Hard Fork." Call podcast_open_spotify. '
    '"Putting the new Hard Fork on Spotify for you. It\'s the one about Nvidia."\n'
    'User: "Too loud." device_command volume_down {steps:2}. Result volume 4 of 15. "Brought it down a bit."\n'
    'User: "What\'s playing?" No action tool. Use portal_state or get_portal_state. '
    '"All-In on Spotify, paused about halfway through."\n'
    'User: "Who founded Nvidia?" No Portal tool. Answer only.\n'
    'User: "Stop." while music plays (Portal already ducked as a reflex). '
    'device_command media_pause. "Paused."\n'
    'User: "Pause." but the result is ok:false / audio none. "Nothing\'s playing on the Portal right now."\n'
    'User: "Turn the Mac volume down." Mac tool (send_task), not device_command.\n'
)

# Portal tools the block names. Mac tools are not in this list.
PORTAL_TOOLS = (
    "device_command",
    "podcast_open_spotify",
    "podcast_follow",
    "podcast_unfollow",
    "podcast_followed",
    "show_panel",
    "dismiss_panel",
    "roku_play",
    "roku_stream",
    "roku_stream_stop",
    "koji_wiki",
    "core_morph",
    "core_say",
    "get_portal_state",
)


def load_contract(path: Path | None = None) -> dict[str, Any]:
    raw = (path or _CONTRACT).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("contract")
    return data


def portal_skill_block(
    contract: dict[str, Any] | None = None,
    tools: tuple[str, ...] | None = None,
    declared: tuple[str, ...] | None = None,
) -> str:
    data = contract if contract is not None else load_contract()
    commands = data.get("device_commands") if isinstance(data.get("device_commands"), list) else []
    names = [item.get("name") for item in commands if isinstance(item, dict) and isinstance(item.get("name"), str)]
    catalog = tools if tools is not None else PORTAL_TOOLS
    if declared is None:
        listed = [name for name in catalog if name]
    else:
        present = set(declared)
        listed = [name for name in catalog if name in present]
    lines = [
        _RULES,
        "Portal actions: " + ", ".join(names) + ".",
        "Portal tools: " + ", ".join(listed) + ".",
        "Transport, volume, brightness, apps, podcasts, Spotify open, panels, Roku, wiki, "
        "and core morph or say-glyph all stay on the Portal. "
        "core_say draws text on the screen and makes no sound. "
        "get_volume returns step, max, and percent.",
        _EXAMPLES,
    ]
    return "\n".join(lines)


def skill_stats(block: str, tools: tuple[str, ...] | None = None) -> tuple[int, int]:
    listed = tools if tools is not None else PORTAL_TOOLS
    return len(listed), len(block.encode("utf-8"))
