"""Canonical, allow-listed Rentium links emitted by RAMA."""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings

DEFAULT_CANONICAL_FRONTEND_ORIGIN = "https://www.rentium.ca"

DASHBOARD_COLLECTIONS: dict[str, tuple[str, str]] = {
    "dashboard": ("Dashboard", "/dashboard"),
    "properties": ("Properties", "/dashboard/properties"),
    "property_groups": ("Property groups", "/dashboard/properties?view=groups"),
    "documents": ("Documents", "/dashboard/documents"),
    "leases": ("Leases", "/dashboard/leases"),
    "finances": ("Finances", "/dashboard/financial"),
    "maintenance": ("Maintenance", "/dashboard/maintenance"),
    "settings": ("Settings", "/dashboard/settings"),
}

COLLECTION_ALIASES = {
    "home": "dashboard",
    "dashboard_home": "dashboard",
    "property_list": "properties",
    "listings": "properties",
    "groups": "property_groups",
    "property_group_list": "property_groups",
    "finance": "finances",
    "financial": "finances",
}


def canonical_frontend_origin() -> str:
    configured = getattr(
        settings,
        "CANONICAL_FRONTEND_ORIGIN",
        DEFAULT_CANONICAL_FRONTEND_ORIGIN,
    )
    value = str(configured or "").strip().rstrip("/")
    parsed = urlparse(value)
    # app.rentium.ca is a legacy redirect, never a URL RAMA should publish.
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname == "app.rentium.ca"
    ):
        return DEFAULT_CANONICAL_FRONTEND_ORIGIN
    return value


def dashboard_collection(key: str) -> tuple[str, str] | None:
    normalised = (key or "").strip().casefold().replace("-", "_").replace(" ", "_")
    normalised = COLLECTION_ALIASES.get(normalised, normalised)
    return DASHBOARD_COLLECTIONS.get(normalised)


def url_for_path(path: str) -> str:
    clean_path = "/" + (path or "").lstrip("/")
    return canonical_frontend_origin() + clean_path
