"""
Chat-completions-compatible adapters. OpenAI and xAI (and most other hosted
providers) speak the same /chat/completions wire format, so xAI is a
two-line subclass — that's the point of the neutral contract.
"""

import json

import requests
from django.conf import settings

from .base import Provider, ProviderError, ToolCall, Turn

TIMEOUT_SECONDS = 60


class OpenAIProvider(Provider):
    name = "openai"
    api_key_setting = "OPENAI_API_KEY"
    base_url = "https://api.openai.com/v1"

    def complete(self, *, model, system, messages, tools):
        api_key = getattr(settings, self.api_key_setting, "")
        if not api_key:
            raise ProviderError(
                f"{self.api_key_setting} is not configured — set it in the "
                f"backend environment to use the {self.name!r} provider."
            )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                *[self._to_wire(m) for m in messages],
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ],
        }
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach the {self.name} API.") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"{self.name} API error {response.status_code}: {response.text[:300]}"
            )

        choice = response.json()["choices"][0]["message"]
        turn = Turn(text=choice.get("content") or "")
        for call in choice.get("tool_calls") or []:
            try:
                arguments = json.loads(call["function"]["arguments"] or "{}")
            except ValueError:
                arguments = {}
            turn.tool_calls.append(
                ToolCall(id=call["id"], name=call["function"]["name"], arguments=arguments)
            )
        return turn

    @staticmethod
    def _to_wire(message):
        role = message["role"]
        if role == "user":
            return {"role": "user", "content": message["content"]}
        if role == "assistant":
            wire = {"role": "assistant", "content": message.get("text") or None}
            if message.get("tool_calls"):
                wire["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"]),
                        },
                    }
                    for c in message["tool_calls"]
                ]
            return wire
        if role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message["tool_call_id"],
                "content": message["content"],
            }
        raise ProviderError(f"Unknown message role {role!r}.")


class XAIProvider(OpenAIProvider):
    name = "xai"
    api_key_setting = "XAI_API_KEY"
    base_url = "https://api.x.ai/v1"
