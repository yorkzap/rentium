"""Anthropic Messages API adapter (the default provider), via the official SDK."""

from django.conf import settings

from .base import Provider, ProviderError, ToolCall, Turn

MAX_TOKENS = 1024  # answers are short prose over tool results, not essays


class AnthropicProvider(Provider):
    name = "anthropic"
    api_key_setting = "ANTHROPIC_API_KEY"

    def complete(self, *, model, system, messages, tools):
        api_key = getattr(settings, self.api_key_setting, "")
        if not api_key:
            raise ProviderError(
                "ANTHROPIC_API_KEY is not configured — set it in the backend "
                "environment to enable RAMA."
            )
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=[
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["parameters"],
                    }
                    for t in tools
                ],
                messages=[self._to_wire(m) for m in messages],
            )
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
