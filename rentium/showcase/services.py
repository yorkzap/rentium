"""
Every public read goes through here. Views are thin; the visibility rule is
Property.objects.public() and lives in ONE place (properties/models.py).
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from django.db.models import Count
from django.db.models import Max
from django.db.models import Min
from django.utils.text import slugify

from rentium.properties.models import PROVINCE_NAMES
from rentium.properties.models import Property

from .models import Showcase
from .models import ShowcaseSlugHistory

# How far to blur a public map marker, in degrees (~0.0025° ≈ 275 m of
# latitude). Deterministic per property, so the pin doesn't jitter on reload
# and can't be averaged out by repeated requests.
JITTER_DEGREES = Decimal("0.0025")


def jittered_coords(prop: Property) -> tuple[float, float] | None:
    """
    A stable, seeded offset from the true coordinates. NEVER return
    prop.latitude/longitude to a logged-out visitor — a precise pin on an
    otherwise-anonymous listing is a street address with extra steps.
    """
    if prop.latitude is None or prop.longitude is None:
        return None

    seed = hashlib.sha256(str(prop.pk).encode()).digest()
    # Map two bytes to [-1, 1] each.
    dx = (seed[0] / 127.5) - 1.0
    dy = (seed[1] / 127.5) - 1.0

    lat = float(prop.latitude) + float(JITTER_DEGREES) * dy
    lng = float(prop.longitude) + float(JITTER_DEGREES) * dx
    return round(lat, 6), round(lng, 6)


# --------------------------------------------------------------- showcases
def resolve_showcase(slug: str) -> tuple[Showcase | None, str | None]:
    """
    -> (showcase, redirect_to_slug)

    A live public slug returns (showcase, None). A retired slug returns
    (showcase, "new-slug") so the caller can 301. A slug for a landlord who
    has since opted OUT returns (None, None) — i.e. a 404, not a redirect.
    Turning your page off must actually turn it off.
    """
    key = (slug or "").strip().lower()
    showcase = (
        Showcase.objects.filter(slug=key, is_public=True)
        .select_related("landlord__user")
        .first()
    )
    if showcase:
        return showcase, None

    old = (
        ShowcaseSlugHistory.objects.filter(slug=key)
        .select_related("showcase__landlord__user")
        .first()
    )
    if old and old.showcase.is_public and old.showcase.slug:
        return old.showcase, old.showcase.slug

    return None, None


def rename_slug(showcase: Showcase, new_slug: str) -> Showcase:
    """Retire the current slug into history, then adopt the new one."""
    new_slug = slugify(new_slug or "")[:60]
    if showcase.slug and showcase.slug != new_slug:
        ShowcaseSlugHistory.objects.get_or_create(
            slug=showcase.slug, defaults={"showcase": showcase}
        )
    # Reclaiming a slug you previously used: drop it from history so it stops
    # redirecting to itself.
    ShowcaseSlugHistory.objects.filter(slug=new_slug, showcase=showcase).delete()
    showcase.slug = new_slug
    showcase.save()
    return showcase


def slug_is_available(slug: str, *, exclude_showcase: Showcase | None = None) -> bool:
    key = slugify(slug or "")[:60]
    if not key or len(key) < 3:
        return False
    from .models import RESERVED_SLUGS

    if key in RESERVED_SLUGS:
        return False
    taken = Showcase.objects.filter(slug=key)
    history = ShowcaseSlugHistory.objects.filter(slug=key)
    if exclude_showcase:
        taken = taken.exclude(pk=exclude_showcase.pk)
        history = history.exclude(showcase=exclude_showcase)
    return not taken.exists() and not history.exists()


# ------------------------------------------------------------------ cities
def city_properties(province_code: str, city_slug: str):
    return (
        Property.objects.public()
        .filter(province_code=province_code.lower(), city_slug=city_slug.lower())
        .select_related("landlord__user", "landlord__showcase")
        .prefetch_related("property_images")
    )


def city_facets(qs) -> dict:
    """Everything the city page's filter UI needs, in one aggregate."""
    agg = qs.aggregate(
        total=Count("id"),
        min_rent=Min("asking_rent"),
        max_rent=Max("asking_rent"),
    )
    by_type = {
        "private_room": qs.filter(
            property_category=Property.PropertyCategory.ROOM,
            room_type=Property.RoomType.PRIVATE,
        ).count(),
        "shared_room": qs.filter(
            property_category=Property.PropertyCategory.ROOM,
            room_type=Property.RoomType.SHARED,
        ).count(),
        "full_suite": qs.filter(
            property_category=Property.PropertyCategory.COMPLETE_UNIT
        ).count(),
    }
    return {
        "total": agg["total"] or 0,
        "min_rent": str(agg["min_rent"]) if agg["min_rent"] is not None else None,
        "max_rent": str(agg["max_rent"]) if agg["max_rent"] is not None else None,
        "counts": by_type,
        "furnished": qs.filter(is_furnished=True).count(),
    }


def known_city(province_code: str, city_slug: str) -> dict | None:
    """
    Does this city exist in our data AT ALL (regardless of current vacancy)?

    A city page must keep rendering — with real, useful content — when it has
    zero available listings, or every vacancy gap silently deletes the page
    from Google and we start again from nothing each time. So we resolve the
    city from ANY property we hold there, public or not, and let the page
    render its evergreen content with an empty results grid.
    """
    prop = (
        Property.objects.filter(
            province_code=province_code.lower(), city_slug=city_slug.lower()
        )
        .only("city", "province_code")
        .first()
    )
    if not prop:
        return None
    return {
        "city": prop.city,
        "city_slug": prop.city_slug,
        "province_code": prop.province_code,
        "province_name": PROVINCE_NAMES.get(prop.province_code, ""),
    }


def all_public_cities() -> list[dict]:
    """For the sitemap and the city index."""
    rows = (
        Property.objects.public()
        .values("city", "city_slug", "province_code")
        .annotate(count=Count("id"))
        .order_by("province_code", "city")
    )
    return [
        {
            **r,
            "province_name": PROVINCE_NAMES.get(r["province_code"], ""),
        }
        for r in rows
        if r["city_slug"] and r["province_code"]
    ]
