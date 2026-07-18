"""
Per-landlord RAMA preferences + API keys.

- Preferences (enabled, provider, model) are owned by the landlord.
- API keys: landlord BYOK first, then platform env (XAI_API_KEY, …).
- Chat memory is landlord-scoped via RamaAudit (conversation_id history).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

DEFAULT_MODELS = {
    "xai": "grok-4-1-fast-reasoning",
    "gemini": "gemini-flash-latest",
    "mistral": "mistral-small-latest",
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
}

MODEL_CATALOG: dict[str, list[dict[str, str]]] = {
    "xai": [
        {"id": "grok-4-1-fast-reasoning", "label": "Grok 4.1 Fast (reasoning)"},
        {"id": "grok-4-1-fast-non-reasoning", "label": "Grok 4.1 Fast"},
        {"id": "grok-4.5", "label": "Grok 4.5 (flagship)"},
    ],
    # Model ids that work for new Google AI Studio keys (some 2.5 names are
    # closed to new users; free tier quotas also vary by model).
    "gemini": [
        {"id": "gemini-flash-latest", "label": "Gemini Flash (latest, recommended)"},
        {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash (preview)"},
        {"id": "gemini-3.1-flash-lite-preview", "label": "Gemini 3.1 Flash Lite (preview)"},
        {"id": "gemini-2.0-flash", "label": "Gemini 2.0 Flash"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
    ],
    "mistral": [
        {"id": "mistral-small-latest", "label": "Mistral Small (recommended)"},
        {"id": "mistral-medium-latest", "label": "Mistral Medium"},
        {"id": "mistral-large-latest", "label": "Mistral Large"},
        {"id": "ministral-8b-latest", "label": "Ministral 8B"},
    ],
    "anthropic": [
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
        {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
    ],
    "openai": [
        {"id": "gpt-4o-mini", "label": "GPT-4o mini"},
        {"id": "gpt-4o", "label": "GPT-4o"},
    ],
}

PROVIDER_ENV_KEYS = {
    "xai": "XAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


@dataclass(frozen=True)
class LandlordRamaConfig:
    enabled: bool
    provider: str
    model: str
    api_key: str  # resolved: BYOK or platform
    has_own_key: bool

    def is_configured(self, provider: str | None = None) -> bool:
        """True when we can call the active (or given) provider for this landlord."""
        if provider and provider != self.provider:
            # For catalog UI: landlord key only applies to their chosen provider;
            # platform key can still light up other options.
            return bool(platform_api_key(provider))
        return bool(self.api_key)


def resolve_model(provider: str, model: str | None) -> str:
    provider = (provider or "xai").strip().lower()
    chosen = (model or "").strip()
    if chosen:
        return chosen
    return DEFAULT_MODELS.get(
        provider, getattr(settings, "RAMA_MODEL", "grok-4-1-fast-reasoning")
    )


def platform_api_key(provider: str) -> str:
    env_name = PROVIDER_ENV_KEYS.get((provider or "").strip().lower(), "")
    if not env_name:
        return ""
    return (getattr(settings, env_name, None) or "") or ""


def get_landlord_config(landlord) -> LandlordRamaConfig:
    """Preferences for this landlord + resolved API key (BYOK → platform)."""
    from .models import RamaPreferences

    prefs = RamaPreferences.for_landlord(landlord)
    provider = (prefs.provider or "xai").strip().lower()
    own = (prefs.api_key or "").strip()
    platform = platform_api_key(provider)
    return LandlordRamaConfig(
        enabled=bool(prefs.enabled),
        provider=provider,
        model=resolve_model(provider, prefs.model),
        api_key=own or platform,
        has_own_key=bool(own),
    )
