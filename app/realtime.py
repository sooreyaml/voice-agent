from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.openai.com/v1"
WS_BASE = "wss://api.openai.com/v1/realtime"


class RealtimeCallError(RuntimeError):
    pass


class RealtimeCalls:
    """Thin wrapper over the /v1/realtime/calls endpoints.

    These control the SIP leg of the call: whether to answer it, where to send
    it, and when to hang up. The audio itself never reaches this process.
    """

    def __init__(self, api_key: str, timeout: float = 10.0):
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> None:
        response = await self._client.post(path, json=payload or {})
        if response.status_code >= 400:
            raise RealtimeCallError(
                f"POST {path} failed with {response.status_code}: {response.text}"
            )

    async def accept(self, call_id: str, session_config: dict[str, Any]) -> None:
        """Answer the call and configure the session that will handle it."""
        await self._post(f"/realtime/calls/{call_id}/accept", session_config)

    async def reject(self, call_id: str, status_code: int = 603) -> None:
        await self._post(f"/realtime/calls/{call_id}/reject", {"status_code": status_code})

    async def refer(self, call_id: str, target_uri: str) -> None:
        """Transfer the caller elsewhere, e.g. target_uri="tel:+16175550143"."""
        await self._post(f"/realtime/calls/{call_id}/refer", {"target_uri": target_uri})

    async def hangup(self, call_id: str) -> None:
        await self._post(f"/realtime/calls/{call_id}/hangup")


def websocket_url(call_id: str) -> str:
    """Sideband control channel for an already-accepted call."""
    return f"{WS_BASE}?call_id={call_id}"


def build_session_config(
    *,
    model: str,
    instructions: str,
    voice: str,
    tools: list[dict[str, Any]],
    transcribe_caller: bool,
    transcribe_model: str,
) -> dict[str, Any]:
    """The payload sent to /accept.

    Audio formats are deliberately omitted: on a SIP call OpenAI negotiates the
    codec with the carrier, and pinning a format here only invites a mismatch.
    """
    audio: dict[str, Any] = {
        "input": {"turn_detection": {"type": "semantic_vad"}},
        "output": {"voice": voice},
    }
    if transcribe_caller:
        audio["input"]["transcription"] = {"model": transcribe_model}

    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "output_modalities": ["audio"],
        "audio": audio,
        "tools": tools,
        "tool_choice": "auto",
    }
