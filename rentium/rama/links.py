"""Canonical, allow-listed Rentium links emitted by RAMA."""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings

DEFAULT_CANONICAL_FRONTEND_ORIGIN = "https://www.rentium.ca"

DASHBOARD_COLLECTIONS: dict[str, tuple[str, str]] = {
    "dashboard": ("Dashboard", "/dashboard"),
    "calendar": ("Calendar", "/dashboard/calendar"),
    "properties": ("Properties", "/dashboard/properties"),
    "property_groups": ("Property groups", "/dashboard/properties?view=groups"),
    "documents": ("Documents", "/dashboard/documents"),
    "leases": ("Leases", "/dashboard/leases"),
    "finances": ("Finances", "/dashboard/financial"),
    "maintenance": ("Maintenance", "/dashboard/maintenance"),
    "inquiries": ("Inquiries", "/dashboard/inquiries"),
    "messages": ("Messages", "/dashboard/messages"),
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
    # There is no "Appointments" nav item — viewings live on Calendar.
    "appointments": "calendar",
    "appointment": "calendar",
    "viewings": "calendar",
    "viewing": "calendar",
    "showings": "calendar",
    "showing": "calendar",
    "schedule": "calendar",
    "visits": "calendar",
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


def public_property_url(property_obj) -> dict:
    """Canonical public route for one listing, plus whether it currently 200s."""
    province = (
        getattr(property_obj, "province_code", "")
        or getattr(property_obj, "province", "")
        or ""
    ).casefold()
    city = getattr(property_obj, "city_slug", "") or ""
    slug = getattr(property_obj, "public_slug", "") or ""
    if not (province and city and slug):
        return {
            "error": (
                "This listing does not have a complete public route yet "
                "(province, city slug, or public slug is missing)."
            )
        }
    from rentium.properties.models import Property

    is_live = Property.objects.public().filter(pk=property_obj.pk).exists()
    return {
        "listing": property_obj.name,
        "link": url_for_path(f"/{province}/{city}/{slug}"),
        "publicly_accessible": is_live,
        "note": (
            "This is the live public listing."
            if is_live
            else (
                "This is the canonical public route, but it is not currently "
                "visible because the portfolio/listing is private or unavailable."
            )
        ),
    }
