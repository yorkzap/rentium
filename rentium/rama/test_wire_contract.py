"""The neutral message format is a contract, not a comment.

August 2026: the stall-recovery round in `service._run_turn_unlocked` appended
`{"role": "user", "text": ...}` where every other user append uses "content".
Both adapters read `message["content"]` unconditionally, so the round raised
KeyError — inside the provider, where only ProviderError is caught. It escaped
`run_turn` and the view, the request 500'd, and because the inbound message had
already been persisted and no outbound ever was, the landlord's question sat in
the thread with no reply under it. The model had produced a correct answer.

The whole suite passed, because ScriptedProvider never looked at message shape.

These tests pin both halves of the fix: the shape is rejected with a catchable
error, and the test doubles can no longer accept what production cannot send.
"""

from __future__ import annotations

import pytest

from rentium.rama.providers.anthropic import AnthropicProvider
from rentium.rama.providers.base import ProviderError
from rentium.rama.providers.base import assistant_message
from rentium.rama.providers.base import tool_message
from rentium.rama.providers.base import user_message
from rentium.rama.providers.base import validate_wire
from rentium.rama.providers.openai_compat import OpenAIProvider
from rentium.rama.providers.testing import assert_translatable


def test_user_message_keyed_text_raises_provider_error_not_key_error():
    """The exact shape that 500'd, and the exact reason it was invisible."""
    bad = [{"role": "user", "text": "look it up and answer now"}]

    with pytest.raises(ProviderError) as excinfo:
        validate_wire(bad)
    assert "content" in str(excinfo.value)

    # The point of raising ProviderError: callers catch it. Confirm the raw
    # adapters really would have raised the uncatchable error instead.
    for adapter in (AnthropicProvider, OpenAIProvider):
        with pytest.raises(KeyError):
            adapter._to_wire(bad[0])


def test_scripted_provider_helper_rejects_the_bad_shape():
    with pytest.raises(ProviderError):
        assert_translatable([{"role": "user", "text": "hi"}])


@pytest.mark.parametrize(
    "message",
    [
        {"role": "wizard", "content": "hi"},
        {"role": None, "content": "hi"},
        "not a dict",
        {"content": "no role at all"},
        {"role": "tool", "name": "read", "content": "{}"},  # no tool_call_id
        {"role": "tool", "tool_call_id": "t1", "name": "read"},  # no content
    ],
)
def test_off_contract_messages_are_rejected(message):
    with pytest.raises(ProviderError):
        validate_wire([message])


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": "how many rents are due?"},
        {"role": "assistant", "text": "Three."},
        {"role": "assistant", "text": ""},  # empty prose is legal
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [{"id": "t1", "name": "read", "arguments": {}}],
        },
        {"role": "tool", "tool_call_id": "t1", "name": "read", "content": "{}"},
    ],
)
def test_valid_messages_translate_through_both_adapters(message):
    assert_translatable([message])


def test_constructors_produce_translatable_messages():
    """Use these instead of dict literals and the bug cannot be written."""
    assert_translatable(
        [
            user_message("how many rents did we receive for aug or are due?"),
            assistant_message(
                "Checking.",
                tool_calls=[{"id": "t1", "name": "read", "arguments": {"entity": "lease"}}],
            ),
            tool_message(tool_call_id="t1", name="read", content="{}"),
            assistant_message("Three received, two due."),
        ],
    )


def test_every_user_append_in_the_turn_loop_uses_content():
    """Guards the whole class, not the one line that was wrong.

    A future append that reintroduces the bug fails here even if no test
    happens to drive that particular recovery branch.
    """
    import re
    from pathlib import Path

    source = Path(__file__).with_name("service.py").read_text()
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r'"role":\s*"user"', line) and '"text"' in line
    ]
    assert offenders == [], offenders
