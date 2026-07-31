"""
Furnishing detection — derived, never hand-entered.

The landlord already tells us what's in a space: InventoryItem (private, per
property) and SharedInventoryItem (per PropertyGroup). "Furnished" is not a
new fact to collect; it's a conclusion to draw from those. So there is no
`is_furnished` checkbox anywhere in the UI — `Property.is_furnished` is a
denormalised cache recomputed by signal whenever inventory changes (see
properties/signals.py), stored only so we can filter on it in SQL for the
public city pages.

The rule, deliberately conservative:

  ROOM           furnished IFF its OWN private inventory contains something
                 you can sleep on. A room with a desk and a lamp but no bed
                 is not a furnished room; a shared sofa in the suite's living
                 room doesn't furnish YOUR room either.

  COMPLETE_UNIT  furnished IFF it has something to sleep on AND at least two
                 other real furnishings (table, sofa, dresser, fridge...).
                 A bare unit with one bed in it isn't "furnished".

Keys, smoke detectors, cleaning supplies, cutlery and linens are excluded
outright — they're inventory (and they matter for the condition inspection
and the roommate agreement's contents list) but they don't furnish anything.
"""

from __future__ import annotations

import re

# Something you can sleep on. Presence of one of these is mandatory for any
# space to count as furnished at all.
SLEEPING_TERMS = {
    "bed",
    "beds",
    "bedframe",
    "mattress",
    "futon",
    "bunk",
    "daybed",
    "murphy bed",
    "sofa bed",
    "sofabed",
    "pull-out",
}

# Real furniture. Counted toward the complete-unit threshold.
FURNITURE_TERMS = {
    "sofa",
    "couch",
    "loveseat",
    "armchair",
    "recliner",
    "chair",
    "stool",
    "table",
    "desk",
    "dresser",
    "wardrobe",
    "closet organizer",
    "nightstand",
    "bedside table",
    "coffee table",
    "dining table",
    "bookshelf",
    "shelf",
    "shelving",
    "cabinet",
    "sideboard",
    "bench",
    "ottoman",
    "tv stand",
    "mirror",
    "lamp",
    "rug",
    "curtain",
    "blind",
}

# Appliances a tenant would otherwise have to buy. Also count toward the
# complete-unit threshold (an unfurnished unit still usually has a stove, so
# these alone never make a place "furnished" — see the rule above).
APPLIANCE_TERMS = {
    "refrigerator",
    "fridge",
    "freezer",
    "stove",
    "oven",
    "range",
    "microwave",
    "dishwasher",
    "washer",
    "dryer",
    "washing machine",
    "television",
    "tv",
    "air conditioner",
    "portable heater",
    "kettle",
    "toaster",
    "coffee maker",
    "vacuum",
}

# Explicitly NOT furnishings, no matter what else they match.
EXCLUDED_TERMS = {
    "key",
    "keys",
    "fob",
    "remote",
    "opener",
    "smoke detector",
    "co detector",
    "carbon monoxide",
    "fire extinguisher",
    "broom",
    "mop",
    "bucket",
    "dustpan",
    "trash bin",
    "garbage",
    "recycling",
    "cleaning",
    "supplies",
    "detergent",
    "soap",
    "hanger",
    "hangers",
    "pillow",
    "pillowcase",
    "bedsheet",
    "sheets",
    "towel",
    "linen",
    "cutlery",
    "utensil",
    "plate",
    "bowl",
    "mug",
    "cup",
    "pot",
    "pan",
    "dish rack",
    "manual",
    "instructions",
}

CATEGORY_SLEEPING = "SLEEPING"
CATEGORY_FURNITURE = "FURNITURE"
CATEGORY_APPLIANCE = "APPLIANCE"
CATEGORY_OTHER = "OTHER"


def _normalise(name: str) -> str:
    # "Bed Frame (Queen) ×2" -> "bed frame queen 2"
    return re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).strip()


def _matches(haystack: str, terms: set[str]) -> bool:
    # Word-boundary match so "keyboard" never matches "key" and
    # "bedroom" never matches "bed".
    for term in terms:
        if re.search(rf"\b{re.escape(term)}\b", haystack):
            return True
    return False


def classify_item(name: str) -> str:
    """Classify one inventory item name into a furnishing category."""
    text = _normalise(name)
    if not text:
        return CATEGORY_OTHER
    if _matches(text, EXCLUDED_TERMS):
        return CATEGORY_OTHER
    if _matches(text, SLEEPING_TERMS):
        return CATEGORY_SLEEPING
    if _matches(text, FURNITURE_TERMS):
        return CATEGORY_FURNITURE
    if _matches(text, APPLIANCE_TERMS):
        return CATEGORY_APPLIANCE
    return CATEGORY_OTHER


def summarise_inventory(items) -> dict:
    """
    {sleeping: [names], furniture: [names], appliances: [names], other: [names]}

    Used by the roommate agreement ("furnished with what, from inventory")
    and by the public property page's "What's included" block. Takes any
    iterable of objects with .name / .quantity.
    """
    buckets: dict[str, list[str]] = {
        CATEGORY_SLEEPING: [],
        CATEGORY_FURNITURE: [],
        CATEGORY_APPLIANCE: [],
        CATEGORY_OTHER: [],
    }
    for item in items:
        qty = getattr(item, "quantity", 1) or 1
        label = item.name if qty <= 1 else f"{item.name} ×{qty}"
        buckets[classify_item(item.name)].append(label)
    return {
        "sleeping": buckets[CATEGORY_SLEEPING],
        "furniture": buckets[CATEGORY_FURNITURE],
        "appliances": buckets[CATEGORY_APPLIANCE],
        "other": buckets[CATEGORY_OTHER],
    }


def compute_is_furnished(prop) -> bool:
    """
    Cache for public filters. True when the landlord declared furnished or
    semi-furnished, OR inventory still has a bed (legacy path). Called by the
    signal in properties/signals.py and recompute_furnishing.
    """
    from .models import InventoryItem
    from .models import Property

    status = getattr(prop, "furnishing_status", None) or ""
    if status in (
        Property.FurnishingStatus.FURNISHED,
        Property.FurnishingStatus.SEMI_FURNISHED,
        "FURNISHED",
        "SEMI_FURNISHED",
    ):
        return True

    items = list(InventoryItem.objects.filter(property=prop).only("name", "quantity"))
    if not items:
        return False

    categories = [classify_item(i.name) for i in items]
    has_bed = CATEGORY_SLEEPING in categories

    if not has_bed:
        return False

    if prop.property_category == Property.PropertyCategory.ROOM:
        # A bed is enough to call a room furnished.
        return True

    # Complete unit: a bed plus at least two other real furnishings.
    supporting = sum(
        1 for c in categories if c in (CATEGORY_FURNITURE, CATEGORY_APPLIANCE)
    )
    return supporting >= 2


def furnishing_label(prop) -> str:
    """Human line for lease PDFs and RAMA."""
    from .models import Property

    status = getattr(prop, "furnishing_status", None) or Property.FurnishingStatus.UNFURNISHED
    try:
        label = prop.get_furnishing_status_display()
    except Exception:  # noqa: BLE001
        label = {
            Property.FurnishingStatus.FURNISHED: "Furnished",
            Property.FurnishingStatus.SEMI_FURNISHED: "Semi-furnished",
            Property.FurnishingStatus.UNFURNISHED: "Unfurnished",
        }.get(status, "Unfurnished")
    details = (getattr(prop, "furnishing_details", None) or "").strip()
    if details:
        return f"{label} — {details}"
    if status == Property.FurnishingStatus.UNFURNISHED and not prop.is_furnished:
        return "Unfurnished — the room comes without furniture."
    if status == Property.FurnishingStatus.SEMI_FURNISHED:
        return "Semi-furnished — see inventory and details below."
    if status == Property.FurnishingStatus.FURNISHED or prop.is_furnished:
        return "Furnished — see what's included below."
    return label
