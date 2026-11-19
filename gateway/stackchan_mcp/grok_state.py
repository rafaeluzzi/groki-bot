# SPDX-License-Identifier: MIT
"""Progress frames for a one-voice Portal. One heard per user turn. Never blocks audio."""

from __future__ import annotations

from typing import Any

GROK_STATES = ("heard", "working", "tool", "done")


class GrokProgress:
    """heard on the first of speech_stopped or committed. done only after audio has ended."""

    def __init__(self) -> None:
        self._heard = False
        self._response_done = False
        self._audio_ended = False

    def on_speech_started(self) -> None:
        # A server VAD split inside one clip must not send a second heard.
        # A new turn resets from done, or from a new koji_context seq.
        self._response_done = False
        self._audio_ended = False

    def on_context(self, seq: int | None) -> None:
        if seq is None or seq == getattr(self, "_seq", None):
            return
        self._seq = seq
        self._heard = False

    def on_user_audio_boundary(self) -> str | None:
        """speech_stopped or input_audio_buffer.committed. The first one wins."""
        if self._heard:
            return None
        self._heard = True
        return "heard"

    def on_response_created(self) -> str:
        self._response_done = False
        self._audio_ended = False
        return "working"

    def on_tool(self) -> str:
        return "tool"

    def on_response_done(self) -> str | None:
        self._response_done = True
        return self._maybe_done()

    def on_audio_ended(self) -> str | None:
        self._audio_ended = True
        return self._maybe_done()

    def _maybe_done(self) -> str | None:
        if not (self._response_done and self._audio_ended):
            return None
        self._heard = False
        self._response_done = False
        self._audio_ended = False
        return "done"


def grok_state_frame(state: str, seq: int | None) -> dict[str, Any]:
    if state not in GROK_STATES:
        raise ValueError("unknown grok state")
    return {"type": "grok_state", "seq": seq, "state": state}
