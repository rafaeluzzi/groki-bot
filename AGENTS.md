# Agent rules

Firmware build, licenses, and C++ conventions: `CLAUDE.md`.

## Koji Portal (gateway/ work for grok-portal)
The Portal app Koji lives in the grok-portal repo. Its `AGENTS.md` has the full rules. The parts that touch this repo:

- One brain: Grok through this gateway. Koji has no model. Do not add a second one.
- Reasoning, knowledge, internet, Mac, and Home Assistant skills are gateway tools. Portal volume, brightness, media, apps, and screen go to Koji as `device_command` or `show_panel`. Never also change the Mac for those.
- `gateway/contract/koji-contract.json` is a copy. The source is grok-portal `contract/koji-contract.json`. Change it there, change `stackchan_mcp/device_commands.py` / `panel_frames.py` and Koji together, then run `python3 tools/sync-contract.py` in grok-portal. `tests/test_koji_contract.py` fails on drift.
- `gateway/wiki/` is the source of the Koji wiki (roles, hand-off, catalog). Edit it here, then run that same sync script to update the grok-portal copies.
- Koji drops a panel or command it cannot parse, silently. A change on one side only is a bug.
- Remotes: `origin` is upstream. Our work goes to `fork` (rafaeluzzi/groki-bot).
- Gateway tests: `cd gateway && STACKCHAN_USB_DISABLE=1 NO_PROXY="*" uv run pytest -q`, with `STACKCHAN_USB_TRANSPORT` unset. New gateway files use `SPDX-License-Identifier: MIT`.
- Live system: never quit Grok Bot from a shell running over its connection. Restart order, only when needed: `com.stackchan.gbot-voice`, then `com.stackchan.gateway`, then `com.grokportal.router`.
