"""Anthropic Messages API adapter, via the official SDK."""

from django.conf import settings

from .base import Provider, ProviderError, ToolCall, Turn, validate_wire

# Plan previews and full set listings routinely exceed 1024 tokens; a cap that
# truncates mid-plan reads as the model "going quiet". Overridable via settings.
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 25


def _max_tokens() -> int:
    return int(getattr(settings, "RAMA_MAX_TOKENS", DEFAULT_MAX_TOKENS))


class AnthropicProvider(Provider):
    name = "anthropic"
    api_key_setting = "ANTHROPIC_API_KEY"

    def complete(self, *, model, system, messages, tools, api_key: str = ""):
        from rentium.rama.runtime import platform_api_key

        validate_wire(messages)
        key = (api_key or "").strip() or platform_api_key(self.name)
        if not key:
            raise ProviderError(
                "No Anthropic API key. Add your key under Account → RAMA, "
                "or set ANTHROPIC_API_KEY on the server."
            )
        import anthropic

        client = anthropic.Anthropic(
            api_key=key,
            timeout=float(
                getattr(
                    settings,
                    "RAMA_PROVIDER_TIMEOUT_SECONDS",
                    DEFAULT_TIMEOUT_SECONDS,
                ),
            ),
        )
        payload = {
            "model": model,
            "max_tokens": _max_tokens(),
            "system": system,
            "messages": [self._to_wire(m) for m in messages],
        }
        # Omitted, not sent empty: a call with no tools means "answer in prose"
        # (the out-of-budget wrap-up), and OpenAI rejects an empty tools array
        # outright. Both adapters treat falsy tools the same way so the caller
        # doesn't have to know which provider it is talking to.
        if tools:
            payload["tools"] = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
                for t in tools
            ]
        try:
            response = client.messages.create(**payload)
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"Anthropic API error {exc.status_code}: {exc.message}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Could not reach the Anthropic API.") from exc

        turn = Turn()
        for block in response.content:
            if block.type == "text":
                turn.text += block.text
            elif block.type == "tool_use":
                turn.tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )
        return turn

    @staticmethod
    def _to_wire(message):
        role = message["role"]
        if role == "user":
            return {"role": "user", "content": message["content"]}
        if role == "assistant":
            blocks = []
            if message.get("text"):
                blocks.append({"type": "text", "text": message["text"]})
            for call in message.get("tool_calls", []):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["arguments"],
                    }
                )
            if not blocks:  # the API rejects empty content
                blocks = [{"type": "text", "text": "…"}]
            return {"role": "assistant", "content": blocks}
        if role == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message["tool_call_id"],
                        "content": message["content"],
                    }
                ],
            }
        raise ProviderError(f"Unknown message role {role!r}.")
