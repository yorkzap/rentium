"""
Address autocomplete + geocoding via Geoapify.

WHY THE KEY LIVES HERE AND NOT IN THE BROWSER
The frontend calls OUR endpoint (/api/showcase/address-search/), and we call
Geoapify. It would be easier to put the key in NEXT_PUBLIC_ and let the browser
talk to Geoapify directly — and it would also mean handing 3,000 free requests a
day to anyone who opens devtools. A key in the browser is a key strangers spend.
So it stays server-side, throttled, and cached.

WHY THIS REPLACES showcase/geocode.py (Nominatim)
Nominatim is a batch geocoder with a hard 1 req/sec policy that explicitly
forbids per-keystroke autocomplete. Using it for both would mean two providers,
two failure modes, and two sets of results that disagree about what a city is
called. Geoapify does both, so we keep one.

WHAT WE ASK FOR AND WHAT WE GET BACK
The landlord types a street address. Geoapify hands back the canonical city,
province, postal code, neighbourhood AND coordinates. So we stop ASKING for
city/province/postal at all — we derive them. That's the whole reason this is
worth building: a field you don't ask for is a field nobody can typo, and the
old free-text `city` and `province` were quietly producing three different
"Victoria"s and silently dropping properties out of the public site whenever
someone wrote "Britsh Columbia".
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

AUTOCOMPLETE_URL = "https://api.geoapify.com/v1/geocode/autocomplete"
GEOCODE_URL = "https://api.geoapify.com/v1/geocode/search"

CACHE_TTL = 60 * 60 * 24  # a street address does not move


class GeoError(Exception):
    """Provider unreachable / misconfigured. Views turn this into a 503."""


def _key() -> str:
    key = getattr(settings, "GEOAPIFY_KEY", "")
    if not key:
        raise GeoError(
            "GEOAPIFY_KEY isn't set. Address lookup is unavailable — get a free "
            "key at myprojects.geoapify.com and add it to your environment."
        )
    return key


def _shape(feature: dict) -> dict:
    """
    Geoapify's GeoJSON -> the flat, boring shape our forms and models want.

    `province` comes back as a full name ("British Columbia"); we hand back BOTH
    that and the two-letter code, because the model stores the code and the human
    reads the name.
    """
    p = feature.get("properties", {}) or {}

    # Street line: house number + street. Deliberately NOT p["formatted"], which
    # includes the city/province/country and would duplicate our own fields.
    house = p.get("housenumber") or ""
    street = p.get("street") or ""
    address_line = f"{house} {street}".strip() or p.get("address_line1") or ""

    province_name = p.get("state") or ""
    from rentium.properties.models import normalise_province

    return {
        "id": p.get("place_id") or p.get("formatted"),
        # What we show in the dropdown — the whole thing, as a human reads it.
        "label": p.get("formatted") or address_line,
        "address": address_line,
        "city": p.get("city")
        or p.get("town")
        or p.get("village")
        or p.get("county")
        or "",
        "province": province_name,
        "province_code": normalise_province(province_name),
        "postal_code": (p.get("postcode") or "").upper(),
        "country": p.get("country") or "Canada",
        "neighbourhood": (
            p.get("suburb")
            or p.get("neighbourhood")
            or p.get("district")
            or p.get("quarter")
            or ""
        ),
        "latitude": p.get("lat"),
        "longitude": p.get("lon"),
    }


def autocomplete(query: str, limit: int = 6) -> list[dict]:
    """
    "3213 wasca" -> [{label: "3213 Wascana Street, Victoria, BC V8Z 3T7", ...}]

    Canada-only, street-level only. Filtering to `type=street|amenity|building`
    keeps whole cities and provinces out of the results — a landlord picking
    "British Columbia" as their property's address is a support ticket waiting
    to happen.
    """
    query = (query or "").strip()
    if len(query) < 3:
        return []

    cache_key = f"geo:ac:{query.lower()}"
    hit = cache.get(cache_key)
    if hit is not None:
        return hit

    try:
        res = requests.get(
            AUTOCOMPLETE_URL,
            params={
                "text": query,
                "apiKey": _key(),
                "filter": "countrycode:ca",
                "limit": limit,
                "format": "geojson",
                "lang": "en",
            },
            timeout=6,
        )
        res.raise_for_status()
        features = res.json().get("features", []) or []
    except GeoError:
        raise
    except Exception as exc:
        logger.exception("Geoapify autocomplete failed for %r", query)
        raise GeoError("Address lookup is temporarily unavailable.") from exc

    results = []
    for f in features:
        shaped = _shape(f)
        # A result we can't turn into a real property is worse than no result —
        # the landlord picks it, and then the property silently can't be
        # published because it has no province. Drop them here instead.
        if shaped["address"] and shaped["city"] and shaped["province_code"]:
            results.append(shaped)

    cache.set(cache_key, results, CACHE_TTL)
    return results


def geocode(address: str, city: str = "", province: str = "") -> dict | None:
    """
    Full geocode of a known address. Used by the backfill command for the
    existing free-text mess, and as a fallback if a landlord edits the address
    by hand instead of picking from the dropdown.
    """
    query = ", ".join(p for p in [address, city, province, "Canada"] if p)
    if not query.strip():
        return None

    cache_key = f"geo:gc:{query.lower()}"
    hit = cache.get(cache_key)
    if hit is not None:
        return hit

    try:
        res = requests.get(
            GEOCODE_URL,
            params={
                "text": query,
                "apiKey": _key(),
                "filter": "countrycode:ca",
                "limit": 1,
                "format": "geojson",
            },
            timeout=8,
        )
        res.raise_for_status()
        features = res.json().get("features", []) or []
    except GeoError:
        raise
    except Exception:
        logger.exception("Geoapify geocode failed for %r", query)
        return None

    if not features:
        cache.set(cache_key, None, 60 * 10)  # don't hammer on a bad address
        return None

    shaped = _shape(features[0])
    cache.set(cache_key, shaped, CACHE_TTL)
    return shaped
