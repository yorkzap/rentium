"""
Web research — the ONLY module that talks to a search provider.

Deliberately pluggable, mirroring comms/whatsapp.py: the provider HTTP calls
are isolated in `_provider_search` / `_provider_fetch`, so moving Firecrawl ↔
Tavily ↔ Exa ↔ Brave is a two-function change and nothing else moves. The
default targets Firecrawl.

Why a search TOOL rather than the model's own knowledge, and why not a
provider-native web search: RAMA is provider-neutral, and Anthropic's
server-side web search, xAI's Live Search and Gemini's grounding are all
different shapes — while Mistral Small has none. Facts that change depending
on which model a landlord picked are worse than no facts, because the
inconsistency is invisible. One tool, identical results on every provider.

Three guards, all deterministic:

1. The model NEVER composes a query or picks a domain. It names a topic key
   from an intervention's `research_topics`; the query template and the domain
   allowlist live here in code.
2. `scrub()` refuses any query carrying tenant names, addresses, or contact
   details. Portfolio PII never leaves the system.
3. `verify_in_source()` requires a figure to appear VERBATIM in the fetched
   text before it can be scored. This is what stops a plausible, confidently
   wrong rebate amount reaching a landlord who is about to spend money.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

from .models import TreasurerSource

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
MAX_EXCERPT_CHARS = 8_000
# Programs change slowly; rates and prices do not.
DEFAULT_TTL_DAYS = 90
RATE_TTL_DAYS = 30
MAX_RESULTS = 5


# ---------------------------------------------------------------------------
# The allowlist. A topic key is all the model may choose — it cannot phrase a
# query, and it cannot reach a domain that is not listed here.
# ---------------------------------------------------------------------------
RESEARCH_TOPICS: dict[str, dict] = {
    "bc_heat_pump_rebate": {
        "query": "BC heat pump rebate amount residential",
        "domains": ("betterhomesbc.ca", "cleanbc.gov.bc.ca", "bchydro.com", "canada.ca"),
        "ttl_days": DEFAULT_TTL_DAYS,
    },
    "cleanbc_income_qualified": {
        "query": "CleanBC income qualified program eligibility rebate",
        "domains": ("betterhomesbc.ca", "cleanbc.gov.bc.ca"),
        "ttl_days": DEFAULT_TTL_DAYS,
    },
    "bc_window_rebate": {
        "query": "BC window replacement rebate amount residential",
        "domains": ("betterhomesbc.ca", "cleanbc.gov.bc.ca", "bchydro.com"),
        "ttl_days": DEFAULT_TTL_DAYS,
    },
    "bc_insulation_rebate": {
        "query": "BC attic insulation rebate amount",
        "domains": ("betterhomesbc.ca", "cleanbc.gov.bc.ca", "bchydro.com"),
        "ttl_days": DEFAULT_TTL_DAYS,
    },
    "bc_mortgage_rates": {
        "query": "Canada five year fixed mortgage rate today",
        "domains": ("bankofcanada.ca", "ratehub.ca"),
        "ttl_days": RATE_TTL_DAYS,
    },
}


def backend() -> str:
    """firecrawl | fake | none. `fake` serves fixtures so evals are
    deterministic and CI needs no network."""
    return (getattr(settings, "RAMA_RESEARCH_BACKEND", "") or "none").strip().lower()


# ------------------------------------------------------------------ guards
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s().-]{7,}\d)(?!\d)")
_POSTAL = re.compile(r"\b[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d\b")
_STREET = re.compile(
    r"\b\d{1,6}\s+\w+(\s+\w+)?\s+(st|street|ave|avenue|rd|road|dr|drive|blvd|way|cres|crescent|lane|ln)\b",
    re.IGNORECASE,
)


def scrub(landlord, query: str) -> str | None:
    """Reason to refuse sending this query outward, or None.

    A search query leaves the system. Anything identifying a tenant or a
    property must not be in it — not because the search would fail, but
    because it would be a disclosure the landlord never agreed to.
    """
    text = (query or "").strip()
    if not text:
        return "Empty query."
    if _EMAIL.search(text):
        return "That query contains an email address."
    if _PHONE.search(text):
        return "That query contains a phone number."
    if _POSTAL.search(text):
        return "That query contains a postal code."
    if _STREET.search(text):
        return "That query contains a street address."

    lowered = text.casefold()
    try:
        from rentium.leases.models import LeaseTenant

        names = (
            LeaseTenant.objects.filter(lease__landlord=landlord)
            .values_list("invited_name", flat=True)
            .distinct()
        )
        for name in names:
            cleaned = str(name or "").strip()
            if len(cleaned) > 2 and cleaned.casefold() in lowered:
                return "That query names one of your tenants."
    except Exception:  # noqa: BLE001 — a scrub failure must not open the gate
        return "Could not verify the query is free of tenant details."
    return None


def verify_in_source(value, source: TreasurerSource) -> bool:
    """Whether a figure actually appears in the page it is credited to.

    Plain string containment, normalised for currency symbols and separators.
    Crude on purpose: a number the model produced that is nowhere in the text
    it cited is an invented number, whatever else is true about it.
    """
    if source is None or not source.excerpt:
        return False
    try:
        amount = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return False

    haystack = re.sub(r"[,\s$]", "", source.excerpt)
    candidates = {
        f"{amount:f}".rstrip("0").rstrip("."),
        f"{amount:.0f}",
        f"{amount:.2f}",
    }
    return any(c and c in haystack for c in candidates)


# ---------------------------------------------------------------- providers
def _provider_search(query: str, *, domains: tuple, limit: int) -> list[dict]:
    """Provider-specific search. DEFAULT: Firecrawl /v2/search.

    Configure via settings/env:
      FIRECRAWL_API_KEY       — the API key
      RAMA_RESEARCH_BACKEND   — firecrawl | fake | none

    To move to Tavily/Exa/Brave, replace this body — `search()` above it does
    not change.
    """
    key = (getattr(settings, "FIRECRAWL_API_KEY", "") or "").strip()
    if not key:
        logger.warning("Research provider not configured; returning nothing")
        return []
    scoped = query
    if domains:
        scoped = f"{query} ({' OR '.join('site:' + d for d in domains)})"
    try:
        response = requests.post(
            "https://api.firecrawl.dev/v2/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": scoped, "limit": limit, "scrapeOptions": {"formats": ["markdown"]}},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — research failing must not break a run
        logger.warning("Research search failed: %s", exc)
        return []

    out = []
    for row in (payload.get("data") or [])[:limit]:
        out.append(
            {
                "url": row.get("url") or "",
                "title": row.get("title") or "",
                "text": (row.get("markdown") or row.get("description") or "")[
                    :MAX_EXCERPT_CHARS
                ],
                "status": 200,
            }
        )
    return out


def _provider_fetch(url: str) -> dict:
    """Provider-specific page fetch. DEFAULT: Firecrawl /v2/scrape."""
    key = (getattr(settings, "FIRECRAWL_API_KEY", "") or "").strip()
    if not key:
        return {}
    try:
        response = requests.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {key}"},
            json={"url": url, "formats": ["markdown"]},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = (response.json() or {}).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Research fetch failed for %s: %s", url, exc)
        return {}
    return {
        "url": url,
        "title": (data.get("metadata") or {}).get("title") or "",
        "text": (data.get("markdown") or "")[:MAX_EXCERPT_CHARS],
        "status": 200,
    }


def _fake_results(topic: str) -> list[dict]:
    """Fixtures for evals and CI. Deterministic, no network."""
    from .research_fixtures import FIXTURES

    return FIXTURES.get(topic, [])


# ------------------------------------------------------------------- public
def _domain_of(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url or "")
    return (match.group(1) if match else "").lower()


def search(landlord, topic: str, *, limit: int = MAX_RESULTS) -> list[TreasurerSource]:
    """Research one ALLOWLISTED topic. The model names the topic; nothing else.

    Returns cached sources when they are still fresh, so a re-run of an
    analysis does not re-fetch and cannot drift.
    """
    spec = RESEARCH_TOPICS.get(topic)
    if spec is None:
        logger.warning("Refused unknown research topic %r", topic)
        return []

    refusal = scrub(landlord, spec["query"])
    if refusal:
        logger.warning("Refused research query for %r: %s", topic, refusal)
        return []

    cached = list(
        TreasurerSource.objects.filter(
            landlord=landlord, topic=topic, status=TreasurerSource.Status.FRESH
        ).filter(expires_at__gt=timezone.now())[:limit]
    )
    if cached:
        return cached

    mode = backend()
    if mode == "none":
        return []
    rows = (
        _fake_results(topic)
        if mode == "fake"
        else _provider_search(spec["query"], domains=spec["domains"], limit=limit)
    )

    ttl = timedelta(days=spec.get("ttl_days", DEFAULT_TTL_DAYS))
    out = []
    for row in rows:
        url = row.get("url") or ""
        domain = _domain_of(url)
        # The allowlist is enforced on the RESULT too — a provider that
        # ignores a site: filter must not smuggle a source in.
        if spec["domains"] and not any(domain.endswith(d) for d in spec["domains"]):
            continue
        text = (row.get("text") or "")[:MAX_EXCERPT_CHARS]
        out.append(
            TreasurerSource.objects.create(
                landlord=landlord,
                topic=topic,
                query=spec["query"],
                url=url,
                title=(row.get("title") or "")[:300],
                domain=domain[:120],
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                excerpt=text,
                fetched_at=timezone.now(),
                expires_at=timezone.now() + ttl,
                http_status=int(row.get("status") or 0),
            )
        )
    return out


def expire_stale(landlord) -> int:
    """Mark anything past its TTL. Evaluated at read time elsewhere too, so
    this is housekeeping rather than correctness."""
    return TreasurerSource.objects.filter(
        landlord=landlord,
        status=TreasurerSource.Status.FRESH,
        expires_at__lte=timezone.now(),
    ).update(status=TreasurerSource.Status.STALE)
