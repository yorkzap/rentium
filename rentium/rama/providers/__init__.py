from .anthropic import AnthropicProvider
from .base import Provider, ProviderError, ToolCall, Turn
from .openai_compat import (
    GeminiProvider,
    MistralProvider,
    OpenAIProvider,
    XAIProvider,
)

PROVIDERS: dict[str, type[Provider]] = {
    AnthropicProvider.name: AnthropicProvider,
    OpenAIProvider.name: OpenAIProvider,
    XAIProvider.name: XAIProvider,
    GeminiProvider.name: GeminiProvider,
    MistralProvider.name: MistralProvider,
}

__all__ = [
    "PROVIDERS",
    "Provider",
    "ProviderError",
    "ToolCall",
    "Turn",
    "get_provider",
]


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]()
    except KeyError:
        available = ", ".join(sorted(PROVIDERS))
        raise ProviderError(
            f"Unknown RAMA provider {name!r}. Available: {available}."
        ) from None
