"""
The provider contract: neutral messages + tool schemas in, one assistant
Turn out. Provider-agnostic by contract, not by promise — anything with
function calling slots in behind this interface, and the active
provider/model is pure configuration (RAMA_PROVIDER / RAMA_MODEL).

Neutral message format (what the chat loop speaks; adapters translate):

    {"role": "user", "content": "<text>"}
    {"role": "assistant", "text": "<text>",
     "tool_calls": [{"id": ..., "name": ..., "arguments": {...}}]}
    {"role": "tool", "tool_call_id": ..., "name": ..., "content": "<json>"}

Neutral tool schema (adapters rename to their wire format):

    {"name": ..., "description": ..., "parameters": <JSON schema>}
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ProviderError(Exception):
    """A provider couldn't complete a turn — bad config or upstream failure."""

    def __init__(self, message: str, *, status_hint: int = 502):
        super().__init__(message)
        # Suggested HTTP status for the chat endpoint (429 rate limit, 400 bad
        # request, 502 upstream, etc.).
        self.status_hint = status_hint


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    # Gemini (and possibly others) attach opaque extra_content that MUST be
    # echoed on the next turn (thought_signature). Opaque to us; pass-through.
    extra: dict = field(default_factory=dict)


@dataclass
class Turn:
    """One assistant turn: prose, tool calls, or both."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def user_message(text: str) -> dict:
    return {"role": "user", "content": text}


def assistant_message(text: str = "", tool_calls: list | None = None) -> dict:
    message = {"role": "assistant", "text": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def tool_message(*, tool_call_id: str, name: str, content: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": content,
    }


#: Keys every adapter reads unconditionally for a given role. A message missing
#: one of these raises KeyError deep inside the adapter, which no caller catches
#: — see validate_wire.
_REQUIRED_KEYS = {
    "user": ("content",),
    "assistant": (),  # text/tool_calls are both optional; adapters use .get()
    "tool": ("tool_call_id", "content"),
}


def validate_wire(messages: list[dict]) -> None:
    """Reject an off-contract message with ProviderError, never KeyError.

    The neutral format above is documented prose that nothing enforced, so an
    append using the wrong key ("text" where a user message needs "content")
    type-checked fine, passed every test — ScriptedProvider never looked at
    message shape — and only failed in production, inside the adapter, as a
    KeyError. Callers catch ProviderError everywhere and KeyError nowhere, so
    that one typo turned a recoverable turn into an HTTP 500 with no reply
    written at all.

    Raising the catchable error here means an unforeseen shape degrades to a
    bad answer instead of to silence.
    """
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ProviderError(
                f"Message {index} is {type(message).__name__}, not a dict.",
                status_hint=500,
            )
        role = message.get("role")
        if role not in _REQUIRED_KEYS:
            raise ProviderError(
                f"Message {index} has unknown role {role!r}; "
                f"expected one of {', '.join(sorted(_REQUIRED_KEYS))}.",
                status_hint=500,
            )
        missing = [key for key in _REQUIRED_KEYS[role] if key not in message]
        if missing:
            raise ProviderError(
                f"Message {index} (role {role!r}) is missing "
                f"{', '.join(missing)}; got keys {sorted(message)}.",
                status_hint=500,
            )


class Provider:
    name = "base"
    api_key_setting = ""  # Django settings attribute holding platform fallback key

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        api_key: str = "",
    ) -> Turn:
        raise NotImplementedError
