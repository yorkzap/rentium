"""Wire-contract enforcement for provider test doubles.

A scripted provider that only records the messages it was handed accepts
shapes no real adapter can translate. That is how a user message carrying its
text under "text" instead of "content" passed the entire suite and then raised
KeyError in production — inside the adapter, where nothing catches it — so the
turn returned HTTP 500 and the landlord got no reply at all.

Every test double's `complete()` should call `assert_translatable(messages)`
first. It runs the real validator plus both adapter families' translators, so
a shape that would break either provider fails in CI instead of in a chat.
"""

from __future__ import annotations

from .base import validate_wire


def assert_translatable(messages) -> None:
    """Raise if any message is off-contract or untranslatable by an adapter."""
    from .anthropic import AnthropicProvider
    from .openai_compat import OpenAIProvider

    validate_wire(messages)
    for message in messages:
        AnthropicProvider._to_wire(message)
        OpenAIProvider._to_wire(message)
