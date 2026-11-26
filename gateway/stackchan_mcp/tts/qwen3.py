"""Qwen3-TTS engine — HTTP client for the resident local service.

The service is expected at ``http://127.0.0.1:8100`` by default and
streams raw ``pcm_s16le`` audio at 24 kHz from ``POST /tts`` with JSON
``{"text": "..."}``. The gateway device pipeline still encodes Opus at
16 kHz, so this engine resamples the returned PCM before handing it to
the orchestrator.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from .audio_utils import DEVICE_SAMPLE_RATE, resample_pcm16_linear
from .base import TTSEngine

logger = logging.getLogger(__name__)


DEFAULT_QWEN3_TTS_URL = "http://127.0.0.1:8100"
DEFAULT_QWEN3_TTS_SAMPLE_RATE = 24000
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0


class Qwen3Engine(TTSEngine):
    """Synthesise text by POSTing to the local Qwen3-TTS HTTP service."""

    name = "qwen3"

    def __init__(
        self,
        url: str | None = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        transport: Any = None,
    ) -> None:
        env_url = os.getenv("STACKCHAN_QWEN3_TTS_URL")
        self._url = (url or env_url or DEFAULT_QWEN3_TTS_URL).rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    @property
    def url(self) -> str:
        """Base URL the engine will connect to. Useful for diagnostics."""
        return self._url

    async def synthesize(self, text: str, **opts: Any) -> AsyncIterator[bytes]:
        """POST text to Qwen3-TTS, stream 16 kHz mono PCM chunks."""
        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised via integration
            raise RuntimeError(
                "httpx is not installed. Install with "
                "'pip install stackchan-mcp[tts]' to enable Qwen3-TTS support."
            ) from exc

        if not isinstance(text, str) or not text.strip():
            raise ValueError("Qwen3 synthesize: 'text' must be a non-empty string")

        payload: dict[str, Any] = {"text": text}
        if opts.get("streaming_interval") is not None:
            payload["streaming_interval"] = opts["streaming_interval"]
        if opts.get("max_tokens") is not None:
            payload["max_tokens"] = opts["max_tokens"]

        timeout = httpx.Timeout(
            connect=self._timeout_seconds,
            read=None,
            write=self._timeout_seconds,
            pool=self._timeout_seconds,
        )
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        saw_pcm = False
        pending = b""
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                async with client.stream(
                    "POST",
                    f"{self._url}/tts",
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        if pending:
                            chunk = pending + chunk
                            pending = b""
                        if len(chunk) % 2:
                            pending = chunk[-1:]
                            chunk = chunk[:-1]
                        if not chunk:
                            continue
                        saw_pcm = True
                        if DEFAULT_QWEN3_TTS_SAMPLE_RATE != DEVICE_SAMPLE_RATE:
                            chunk = resample_pcm16_linear(
                                chunk,
                                DEFAULT_QWEN3_TTS_SAMPLE_RATE,
                                DEVICE_SAMPLE_RATE,
                            )
                        yield chunk
        except BrokenPipeError as exc:
            raise RuntimeError("Qwen3-TTS stream broke") from exc

        if pending:
            raise RuntimeError("Qwen3-TTS returned an incomplete PCM sample")
        if not saw_pcm:
            raise RuntimeError("Qwen3-TTS returned empty PCM response")

        logger.info("Qwen3-TTS streamed PCM chars=%s", len(text))
