"""
Chat-completions-compatible adapters. OpenAI and xAI (and most other hosted
providers) speak the same /chat/completions wire format, so xAI is a
two-line subclass — that's the point of the neutral contract.
"""

import json

import requests
from django.conf import settings

from .base import Provider, ProviderError, ToolCall, Turn, validate_wire

TIMEOUT_SECONDS = 25
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.0  # RAMA routes and phrases; determinism beats flair


class OpenAIProvider(Provider):
    name = "openai"
    api_key_setting = "OPENAI_API_KEY"
    base_url = "https://api.openai.com/v1"

    def complete(self, *, model, system, messages, tools, api_key: str = ""):
        from rentium.rama.runtime import platform_api_key

        validate_wire(messages)
        key = (api_key or "").strip() or platform_api_key(self.name)
        if not key:
            raise ProviderError(
                f"No API key for {self.name!r}. Add your key under Account → RAMA, "
                f"or ask the operator to set {self.api_key_setting} on the server."
            )
        api_key = key
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
        max_tokens = int(
            getattr(settings, "RAMA_MAX_TOKENS", DEFAULT_MAX_TOKENS)
        )
        if self.name == "openai":
            # Current OpenAI reasoning models (including gpt-5-mini) reject
            # the legacy ``max_tokens`` field and non-default temperatures.
            # Keep those compatibility choices scoped to OpenAI: xAI,
            # Gemini and Mistral share this adapter but retain their existing
            # OpenAI-compatible request contracts.
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens
            payload["temperature"] = float(
                getattr(settings, "RAMA_TEMPERATURE", DEFAULT_TEMPERATURE)
            )
        # Trailing slash on base_url must not produce //chat/completions —
        # Gemini's OpenAI-compat gateway 404s on the double slash.
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=int(
                    getattr(
                        settings,
                        "RAMA_PROVIDER_TIMEOUT_SECONDS",
                        TIMEOUT_SECONDS,
                    ),
                ),
            )
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach the {self.name} API.") from exc
        if response.status_code >= 400:
            # _format_http_error raises ProviderError with a status_hint.
            self._raise_http_error(response)

        try:
            choice = response.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ProviderError(
                f"{self.name} returned an unexpected response."
            ) from exc
        turn = Turn(text=choice.get("content") or "")
        for call in choice.get("tool_calls") or []:
            try:
                arguments = json.loads(call["function"]["arguments"] or "{}")
            except ValueError:
                arguments = {}
            # Preserve Gemini thought_signature / extra_content for multi-turn
            # tool loops. Dropping it causes 400 on the next complete().
            extra = {}
            if isinstance(call.get("extra_content"), dict):
                extra["extra_content"] = call["extra_content"]
            turn.tool_calls.append(
                ToolCall(
                    id=call["id"],
                    name=call["function"]["name"],
                    arguments=arguments,
                    extra=extra,
                )
            )
        return turn

    def _raise_http_error(self, response) -> None:
        """Turn provider HTTP errors into a ProviderError a landlord can act on."""
        status = response.status_code
        body = (response.text or "")[:500]
        try:
            data = response.json()
        except ValueError:
            data = None

        # Normalize: some Gemini OpenAI-compat errors are a one-item list.
        if isinstance(data, list) and data:
            data = data[0]

        # xAI: {"code":"permission-denied","error":"Your newly created team..."}
        # OpenAI/Gemini: {"error":{"message":"..."}}
        message = ""
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, str):
                message = err
            elif isinstance(err, dict):
                message = str(err.get("message") or err.get("error") or "")
            if not message:
                message = str(data.get("message") or "")

        message = (message or body or "unknown error").strip()
        lower = message.lower()

        if status in (401, 403):
            if "credit" in lower or "license" in lower or "billing" in lower:
                raise ProviderError(
                    f"{self.name} needs credits/billing on the provider account. "
                    "Add billing in the console, then retry.",
                    status_hint=403,
                )
            if "invalid" in lower or "auth" in lower or "api key" in lower:
                raise ProviderError(
                    f"{self.name} rejected the API key. Paste a fresh key under "
                    "Account → RAMA.",
                    status_hint=401,
                )
            raise ProviderError(
                f"{self.name} denied the request: {message[:200]}",
                status_hint=403,
            )

        if status == 404:
            if "no longer available" in lower or "newer model" in lower:
                raise ProviderError(
                    f"{self.name}: that model isn't available on your key. "
                    "Under Account → RAMA pick Gemini Flash (latest).",
                    status_hint=400,
                )
            raise ProviderError(
                f"{self.name} does not recognize that model. Try another under "
                "Account → RAMA.",
                status_hint=400,
            )
        if status == 429:
            raise ProviderError(
                f"{self.name} rate limit / free-tier quota hit. Wait a minute, "
                "or enable paid billing in Google AI Studio / your provider "
                "console. Tool-heavy questions use more quota than short chat.",
                status_hint=429,
            )
        raise ProviderError(
            f"{self.name} API error {status}: {message[:220]}",
            status_hint=502,
        )

    @staticmethod
    def _to_wire(message):
        role = message["role"]
        if role == "user":
            return {"role": "user", "content": message["content"]}
        if role == "assistant":
            wire = {"role": "assistant", "content": message.get("text") or None}
            if message.get("tool_calls"):
                wire_calls = []
                for c in message["tool_calls"]:
                    item = {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"]),
                        },
                    }
                    # Echo provider-specific blobs (Gemini thought_signature).
                    extra = c.get("extra") or {}
                    if isinstance(extra, dict) and extra.get("extra_content"):
                        item["extra_content"] = extra["extra_content"]
                    wire_calls.append(item)
                wire["tool_calls"] = wire_calls
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


class GeminiProvider(OpenAIProvider):
    """Google Gemini via the OpenAI-compatible Chat Completions surface.

    Only an API key is required (from Google AI Studio). Project name/number
    are already bound to the key — users never need to enter them.
    Docs: https://ai.google.dev/gemini-api/docs/openai
    """

    name = "gemini"
    api_key_setting = "GEMINI_API_KEY"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"


class MistralProvider(OpenAIProvider):
    """Mistral AI via the OpenAI-compatible Chat Completions API.

    API key only (console.mistral.ai). Tool/function calling is supported on
    current chat models (e.g. mistral-small, mistral-medium, mistral-large).
    Docs: https://docs.mistral.ai/api/
    """

    name = "mistral"
    api_key_setting = "MISTRAL_API_KEY"
    base_url = "https://api.mistral.ai/v1"
