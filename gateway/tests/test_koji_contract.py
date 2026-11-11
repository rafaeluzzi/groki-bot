# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""The gateway side of gateway/contract/koji-contract.json.

The file is a copy of grok-portal ``contract/koji-contract.json``. Koji runs the
same file in ``ContractTest.kt``. Edit it in grok-portal and copy it here with
``python3 tools/sync-contract.py`` from the grok-portal checkout.
"""

import json
from pathlib import Path

import pytest

from stackchan_mcp import panel_frames
from stackchan_mcp.panel_templates import TEMPLATE_NAMES
from stackchan_mcp.device_commands import (
    DEVICE_COMMAND_ARGS,
    DEVICE_COMMAND_NAMES,
    build_device_command_frame,
    device_command_parameters,
)

GATEWAY = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((GATEWAY / "contract" / "koji-contract.json").read_text(encoding="utf-8"))
COMMANDS = CONTRACT["device_commands"]
PANEL = CONTRACT["panel"]


def send(name, args):
    return build_device_command_frame("device_command", {"name": name, "args": args})


def smallest(command):
    return {
        key: spec["min"] if spec["type"] == "int" else spec["values"][0]
        for key, spec in command["args"].items()
        if spec.get("required")
    }


def test_command_names_and_arguments_match_the_contract():
    assert list(DEVICE_COMMAND_NAMES) == [c["name"] for c in COMMANDS]
    assert DEVICE_COMMAND_ARGS == {c["name"]: c["args"] for c in COMMANDS}


def test_the_tool_schema_declares_every_contract_argument():
    props = device_command_parameters("device_command")["properties"]["args"]["properties"]
    for command in COMMANDS:
        for key, spec in command["args"].items():
            assert key in props, (command["name"], key)
            if spec["type"] == "enum":
                assert props[key]["enum"] == spec["values"]


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c["name"])
def test_contract_calls_are_sent_and_out_of_contract_calls_are_not(command):
    name, base = command["name"], smallest(command)
    assert send(name, base) == {"type": "device_command", "name": name, "args": base}
    assert send(name, {**base, "not_in_contract": 1}) is None
    for key, spec in command["args"].items():
        if spec.get("required"):
            assert send(name, {k: v for k, v in base.items() if k != key}) is None
        if spec["type"] == "int":
            for ok in (spec["min"], spec["max"]):
                assert send(name, {**base, key: ok}) is not None
            for bad in (spec["min"] - 1, spec["max"] + 1, str(spec["min"]), True):
                assert send(name, {**base, key: bad}) is None
        else:
            for ok in spec["values"]:
                assert send(name, {**base, key: ok}) is not None
            assert send(name, {**base, key: "not_in_contract"}) is None


@pytest.mark.parametrize("path", ["PORTAL_DEVICE_COMMANDS.md", "wiki/pages/catalog.md"])
def test_command_docs_list_every_name(path):
    text = (GATEWAY / path).read_text(encoding="utf-8")
    for command in COMMANDS:
        assert f"`{command['name']}`" in text, (path, command["name"])


def test_panel_kinds_pattern_and_limits_match_the_contract():
    limits = PANEL["limits"]
    assert PANEL["version"] == 1
    assert list(panel_frames.PANEL_KINDS) == PANEL["kinds"]
    assert panel_frames._TOKEN_RE.pattern == PANEL["id_pattern"]
    assert {
        "rows": panel_frames._MAX_ROWS,
        "actions": panel_frames._MAX_ACTIONS,
        "title": panel_frames._MAX_TITLE,
        "row_label": panel_frames._MAX_TITLE,
        "subtitle": panel_frames._MAX_SUBTITLE,
        "prompt": panel_frames._MAX_PROMPT,
        "action_label": panel_frames._MAX_ACTION_LABEL,
        "url": panel_frames._MAX_URL,
        "seconds": panel_frames._MAX_SECONDS,
    } == limits
    assert panel_frames.DEFAULT_TTL_S == {key: int(value) for key, value in PANEL["ttl_s_default"].items()}
    assert TEMPLATE_NAMES == tuple(PANEL["templates"])


@pytest.mark.parametrize("rebuild", PANEL["rebuilds"], ids=lambda r: r["name"])
def test_loose_documents_rebuild_to_what_koji_parses(rebuild):
    doc, dropped = panel_frames.normalize_panel_document(rebuild["input"])
    assert (doc, dropped) == (rebuild["output"], 0)
    assert panel_frames.normalize_panel_document(rebuild["output"]) == (rebuild["output"], 0)
