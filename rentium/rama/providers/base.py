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
