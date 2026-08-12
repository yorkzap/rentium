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
    "xai": "grok-4.3",
    "gemini": "gemini-flash-latest",
    "mistral": "mistral-small-latest",
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-5-mini",
}

# Curated per vendor: one fast/cheap option (the recommended default — RAMA is
# designed to work on weak models) and the vendor's bigger tiers for landlords
# who want them. BYOK landlords can type any uncataloged id (resolve_model
# passes it through). Verified against vendor docs 2026-07.
MODEL_CATALOG: dict[str, list[dict[str, str]]] = {
    # grok-4-1-fast-* were retired 2026-05-15 (requests redirect to grok-4.3).
    "xai": [
        {"id": "grok-4.3", "label": "Grok 4.3 (fast, recommended)"},
        {"id": "grok-4.5", "label": "Grok 4.5 (flagship — not available in EU)"},
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
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5 (fast, recommended)"},
        {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
        {"id": "claude-opus-4-8", "label": "Claude Opus 4.8"},
        {"id": "claude-fable-5", "label": "Claude Fable 5 (most capable, premium)"},
    ],
    "openai": [
        {"id": "gpt-5-mini", "label": "GPT-5 mini (cheap, recommended)"},
        {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna (fast)"},
        {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra (balanced)"},
        {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol (frontier)"},
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
        provider, getattr(settings, "RAMA_MODEL", "grok-4.3")
    )


def is_model_certified(provider: str, model: str) -> bool:
    """Whether this model id passed the curated RAMA tool-planning contract.

    Landlords may still use an arbitrary provider model for chat and reads.
    Unknown ids do not receive model-authored write tools until certified;
    deterministic server routes and explicit plan execution remain available.
    """
    return any(
        row["id"] == (model or "").strip()
        for row in MODEL_CATALOG.get((provider or "").strip().lower(), [])
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


# Role-default models when the landlord hasn't picked one: the General gets
# the provider's strong tier (it reasons over policy and makes decisions), the
# FSA a mid tier (bounded analyses over prepared fact packs). Corporals keep
# the landlord's cheap chat model — that's the cost pyramid.
GENERAL_DEFAULT_MODELS = {
    "mistral": "mistral-large-latest",
    "anthropic": "claude-sonnet-5",
    "xai": "grok-4.5",
    "openai": "gpt-5.6-terra",
    "gemini": "gemini-flash-latest",
}
FSA_DEFAULT_MODELS = {
    "mistral": "mistral-medium-latest",
    "anthropic": "claude-haiku-4-5",
    "xai": "grok-4.3",
    "openai": "gpt-5.6-luna",
    "gemini": "gemini-flash-latest",
}
# The Treasurer runs long, structured, multi-pass work, so it wants a model
# that is cheap per call and reliable at following a narrow contract rather
# than one that is clever in a single shot. Gemini Flash is the default tier
# for exactly that reason. Switching a landlord to Anthropic or xAI must not
# change WHAT it concludes — the reasoning structure is Python; the model only
# fills in bounded slots — so a different provider should read differently,
# never decide differently.
TREASURER_DEFAULT_MODELS = {
    "gemini": "gemini-flash-latest",
    "mistral": "mistral-medium-latest",
    "anthropic": "claude-haiku-4-5",
    "xai": "grok-4.3",
    "openai": "gpt-5.6-luna",
}
# Provider fallback when the landlord has expressed no preference for the role.
# Everything else inherits the landlord's chat provider; the Treasurer is the
# one role with an opinion of its own.
ROLE_PREFERRED_PROVIDERS = {"treasurer": "gemini"}
_ROLE_DEFAULTS = {
    "general": GENERAL_DEFAULT_MODELS,
    "fsa": FSA_DEFAULT_MODELS,
    "treasurer": TREASURER_DEFAULT_MODELS,
}


def get_role_config(landlord, role: str) -> LandlordRamaConfig:
    """Provider/model for one agent role (corporal | general | fsa | treasurer).

    Resolution: landlord's per-role prefs → platform RAMA_<ROLE>_* settings →
    the landlord's chat provider with the role's default model tier. BYOK:
    the landlord's key applies when the role's provider matches their chat
    provider; otherwise the platform key for that provider is used.
    """
    from .models import RamaPreferences

    chat = get_landlord_config(landlord)
    if role == "corporal" or role not in _ROLE_DEFAULTS:
        return chat

    prefs = RamaPreferences.for_landlord(landlord)
    role_provider = (getattr(prefs, f"{role}_provider", "") or "").strip().lower()
    # A role with its own preferred provider (the Treasurer wants Gemini) uses
    # it only when nothing has been configured AND that provider can actually
    # be called. Otherwise fall back to the chat provider rather than routing
    # to a model there is no key for.
    preferred = ROLE_PREFERRED_PROVIDERS.get(role, "")
    if preferred and not platform_api_key(preferred):
        preferred = ""
    provider = (
        role_provider
        or (getattr(settings, f"RAMA_{role.upper()}_PROVIDER", "") or "").strip().lower()
        or preferred
        or chat.provider
    )
    # A per-role MODEL only applies alongside its per-role PROVIDER — otherwise a
    # leftover model name (e.g. "claude-sonnet-5") would be sent to whatever
    # provider we fell back to (e.g. Mistral) and rejected as an invalid model.
    role_model = (getattr(prefs, f"{role}_model", "") or "").strip() if role_provider else ""
    model = (
        role_model
        or (getattr(settings, f"RAMA_{role.upper()}_MODEL", "") or "").strip()
        or _ROLE_DEFAULTS[role].get(provider, "")
    )
    # Key priority: this role's own BYOK key → the main key (only if the role
    # uses the same provider as the corporal) → the platform key. This is what
    # lets the General/FSA run a different provider than the corporal.
    own = (getattr(prefs, f"{role}_api_key", "") or "").strip()
    if not own and provider == chat.provider:
        own = (prefs.api_key or "").strip()
    return LandlordRamaConfig(
        enabled=chat.enabled,
        provider=provider,
        model=resolve_model(provider, model),
        api_key=own or platform_api_key(provider),
        has_own_key=bool(own),
    )
