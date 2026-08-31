"""A stand-in for OpenAI's Realtime API, so a whole call can run offline.

It speaks the same event sequence a real SIP call produces: the agent greeting,
caller transcripts, a tool call, and finally end_call. Anything the client sends
back is recorded so tests can assert on it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Self

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

# Usage numbers in the shape the Realtime API reports them: roughly one minute
# of the caller talking and a few seconds of the agent replying.
USAGE = {
    "input_token_details": {
        "audio_tokens": 600,
        "text_tokens": 1200,
        "cached_tokens_details": {"audio_tokens": 0, "text_tokens": 1100},
    },
    "output_token_details": {"audio_tokens": 240, "text_tokens": 12},
}

GREETING = (
    "Thanks for calling Harborview Dental, this is Alex. How can I help you today?"
)
CALLER_ASKS = "Hi, how much does a cleaning cost?"
AGENT_ANSWERS = "A routine hygiene visit is one hundred and twenty dollars."
CALLER_BOOKS = "Great, can someone call me back to book? I'm Dana, on 617 555 0188."
AGENT_GOODBYE = "Someone will call you back shortly. Thanks for calling, goodbye."


@dataclass
class Recorder:
    """Everything the client sent us."""

    received: list[dict[str, Any]] = field(default_factory=list)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.received if m.get("type") == kind]

    @property
    def tool_outputs(self) -> list[dict[str, Any]]:
        out = []
        for msg in self.of_type("conversation.item.create"):
            item = msg.get("item") or {}
            if item.get("type") == "function_call_output":
                out.append(item)
        return out


def _transcript(text: str) -> str:
    return json.dumps(
        {"type": "response.output_audio_transcript.done", "transcript": text}
    )


def _caller(text: str) -> str:
    return json.dumps(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": text,
        }
    )


def _done(function_calls: list[dict[str, Any]] | None = None) -> str:
    output = []
    for index, call in enumerate(function_calls or []):
        output.append(
            {
                "type": "function_call",
                "name": call["name"],
                "call_id": f"call_{index}",
                "arguments": json.dumps(call.get("arguments", {})),
            }
        )
    return json.dumps(
        {"type": "response.done", "response": {"usage": USAGE, "output": output}}
    )


async def _script(ws, rec: Recorder) -> None:
    async def recv() -> dict[str, Any]:
        message = json.loads(await ws.recv())
        rec.received.append(message)
        return message

    # The client opens by asking for the fixed greeting.
    await recv()
    await ws.send(_transcript(GREETING))
    await ws.send(_done())

    # Caller asks a question the business config can answer.
    await ws.send(_caller(CALLER_ASKS))
    await ws.send(_transcript(AGENT_ANSWERS))
    await ws.send(_done())

    # Caller states what they want, and the model records it.
    await ws.send(_caller(CALLER_BOOKS))
    await ws.send(
        _done(
            [
                {
                    "name": "capture_caller_need",
                    "arguments": {
                        "intent": "book_appointment",
                        "details": "Wants a callback to book a hygiene visit.",
                        "caller_name": "Dana",
                        "callback_number": "+16175550188",
                        "urgency": "normal",
                    },
                }
            ]
        )
    )
    await recv()  # function_call_output
    await recv()  # response.create, handing control back to the model

    # Agent wraps up and hangs up.
    await ws.send(_transcript(AGENT_GOODBYE))
    await ws.send(
        _done([{"name": "end_call", "arguments": {"reason": "caller satisfied"}}])
    )
    await recv()  # function_call_output for end_call

    # The client should now hang up rather than ask for another response.
    try:
        await asyncio.wait_for(recv(), timeout=2.0)
    except (asyncio.TimeoutError, ConnectionClosed):
        pass


CALLER_WANTS_SLOT = "I'd like to book a cleaning for the first of June please."
AGENT_OFFERS_SLOT = "I have three in the afternoon on Monday the first of June."
CALLER_PICKS_SLOT = "Two o'clock works. I'm Dana on 617 555 0188."
AGENT_CONFIRMS_BOOKING = "You're booked for two o'clock on Monday the first of June."

BOOK_START = "2099-06-01T14:00:00+00:00"


async def _calendar_script(ws, rec: Recorder) -> None:
    """The caller books an appointment through the calendar tools."""

    async def recv() -> dict[str, Any]:
        message = json.loads(await ws.recv())
        rec.received.append(message)
        return message

    await recv()
    await ws.send(_transcript(GREETING))
    await ws.send(_done())

    await ws.send(_caller(CALLER_WANTS_SLOT))
    await ws.send(
        _done(
            [
                {
                    "name": "check_availability",
                    "arguments": {"date": "2099-06-01", "part_of_day": "any"},
                }
            ]
        )
    )
    await recv()  # function_call_output for check_availability
    await recv()  # response.create

    await ws.send(_transcript(AGENT_OFFERS_SLOT))
    await ws.send(_caller(CALLER_PICKS_SLOT))
    await ws.send(
        _done(
            [
                {
                    "name": "book_appointment",
                    "arguments": {
                        "start": BOOK_START,
                        "caller_name": "Dana",
                        "callback_number": "+16175550188",
                        "notes": "hygiene visit",
                    },
                }
            ]
        )
    )
    await recv()  # function_call_output for book_appointment
    await recv()  # response.create

    await ws.send(_transcript(AGENT_CONFIRMS_BOOKING))
    await ws.send(
        _done([{"name": "end_call", "arguments": {"reason": "appointment booked"}}])
    )
    await recv()  # function_call_output for end_call
    try:
        await asyncio.wait_for(recv(), timeout=2.0)
    except (asyncio.TimeoutError, ConnectionClosed):
        pass


CALLER_IDENTIFIES = "Hi, it's Dana calling about my recent order."
AGENT_RECOGNISES = "Welcome back, Dana. How can I help with your order?"
CALLER_WANTS_REFUND = "I'd like someone to call me about a refund, on 617 555 0188."
AGENT_LOGS_FOLLOWUP = (
    "I've logged that and someone will call you back about the refund."
)


async def _crm_script(ws, rec: Recorder) -> None:
    """The caller is recognised from the CRM and asks for a follow-up."""

    async def recv() -> dict[str, Any]:
        message = json.loads(await ws.recv())
        rec.received.append(message)
        return message

    await recv()
    await ws.send(_transcript(GREETING))
    await ws.send(_done())

    await ws.send(_caller(CALLER_IDENTIFIES))
    await ws.send(_done([{"name": "find_customer", "arguments": {}}]))
    await recv()  # function_call_output for find_customer
    await recv()  # response.create

    await ws.send(_transcript(AGENT_RECOGNISES))
    await ws.send(_caller(CALLER_WANTS_REFUND))
    await ws.send(
        _done(
            [
                {
                    "name": "create_follow_up",
                    "arguments": {
                        "summary": "Wants a callback about a refund on the last order.",
                        "caller_name": "Dana",
                        "callback_number": "+16175550188",
                    },
                }
            ]
        )
    )
    await recv()  # function_call_output for create_follow_up
    await recv()  # response.create

    await ws.send(_transcript(AGENT_LOGS_FOLLOWUP))
    await ws.send(
        _done([{"name": "end_call", "arguments": {"reason": "follow-up logged"}}])
    )
    await recv()  # function_call_output for end_call
    try:
        await asyncio.wait_for(recv(), timeout=2.0)
    except (asyncio.TimeoutError, ConnectionClosed):
        pass


CALLER_IN_PAIN = "I'm in a lot of pain, can I please speak to someone?"
AGENT_TRANSFERS = "I'm sorry to hear that. Let me put you through to a colleague now."


async def _transfer_script(ws, rec: Recorder) -> None:
    """The caller needs a human, so the agent hands the call off."""

    async def recv() -> dict[str, Any]:
        message = json.loads(await ws.recv())
        rec.received.append(message)
        return message

    await recv()
    await ws.send(_transcript(GREETING))
    await ws.send(_done())

    await ws.send(_caller(CALLER_IN_PAIN))
    await ws.send(_transcript(AGENT_TRANSFERS))
    await ws.send(
        _done(
            [{"name": "transfer_to_human", "arguments": {"reason": "caller in pain"}}]
        )
    )
    await recv()  # function_call_output

    # A real transfer takes the caller off this session, so the socket closes.
    try:
        await asyncio.wait_for(recv(), timeout=0.3)
    except (asyncio.TimeoutError, ConnectionClosed):
        pass


SCRIPTS = {
    "default": _script,
    "transfer": _transfer_script,
    "calendar": _calendar_script,
    "crm": _crm_script,
}


class FakeRealtime:
    """Async context manager yielding a ws:// URL the session can connect to.

    `reject_first` refuses that many handshakes before serving the script, which
    is how the real API behaves for a moment after /accept: the call exists but
    is not attached to a session yet, so the upgrade comes back 404.
    """

    def __init__(
        self,
        scenario: str = "default",
        reject_first: int = 0,
        reject_status: int = 404,
    ) -> None:
        self.recorder = Recorder()
        self.scenario = scenario
        self.rejected = 0
        # The library's own Headers mapping, so lookups stay case-insensitive
        # the way HTTP header names are.
        self.handshake_headers: Any = {}
        self._reject_first = reject_first
        self._reject_status = reject_status
        self._server: Any = None
        self.url = ""

    async def __aenter__(self) -> Self:
        script = SCRIPTS[self.scenario]

        async def handler(ws):
            try:
                await script(ws, self.recorder)
            except ConnectionClosed:
                pass

        def process_request(connection, request):
            self.handshake_headers = request.headers
            if self.rejected < self._reject_first:
                self.rejected += 1
                return connection.respond(self._reject_status, "call not attached\n")
            return None

        self._server = await serve(
            handler, "127.0.0.1", 0, process_request=process_request
        )
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/realtime"
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()
