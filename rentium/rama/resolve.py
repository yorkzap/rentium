"""
Shared landlord-scoped resolvers for RAMA tools.

Property queries accept:
  - UUID or numeric primary key
  - exact / partial name
  - optional pick= when multiple match: first | last | with_group | no_group |
    with_lease | no_lease | 1 | 2 | … (1-based index in candidates list)
"""

from __future__ import annotations

import uuid
from typing import Any


# Natural-language → canonical pick token. Lets the landlord say "the old one"
# and have the deterministic resolver pick it, instead of leaning on the weak
# model to translate. "matches" is ordered oldest→newest (created_at, pk), so
# oldest == first, newest == last.
_PICK_ALIASES = {
    "old": "oldest", "older": "oldest", "oldest": "oldest",
    "earliest": "oldest", "earlier": "oldest", "original": "oldest",
    "first": "first", "1st": "first",
    "new": "newest", "newer": "newest", "newest": "newest",
    "latest": "newest", "recent": "newest", "most_recent": "newest",
    "last": "last", "2nd": "last", "second": "last",
    # "operate on every duplicate" escape hatch for destructive bulk ops.
    "all": "all", "both": "all", "every": "all", "each": "all",
}


def _normalize_pick(pick: str) -> str:
    """Map free-text ('the old one', 'newer', 'second') to a canonical pick."""
    p = (pick or "").strip().lower()
    if not p:
        return ""
    # Strip common filler so "the old one" / "old room" reduce to a keyword.
    for filler in ("the ", " one", " one.", " room", " listing", " unit"):
        p = p.replace(filler, "")
    p = p.strip()
    if p in _PICK_ALIASES:
        return _PICK_ALIASES[p]
    # Bare word inside a longer phrase, e.g. "old" survives above; also catch
    # any remaining alias token embedded in the phrase.
    for word in p.split():
        if word in _PICK_ALIASES:
            return _PICK_ALIASES[word]
    return p


def _candidate_row(prop) -> dict[str, Any]:
    from rentium.leases.models import Lease

    lease_count = Lease.objects.filter(property=prop).count()
    open_leases = (
        Lease.objects.filter(property=prop)
        .exclude(status__in=["TERMINATED", "EXPIRED"])
        .count()
    )
    return {
        "id": str(prop.pk),
        "name": prop.name,
        "address": prop.address,
        "city": prop.city,
        "group": prop.group.name if prop.group_id else None,
        "group_id": str(prop.group_id) if prop.group_id else None,
        "status": prop.status,
        "inventory_count": prop.inventory_items.count(),
        "lease_count": lease_count,
        "open_lease_count": open_leases,
        "created_at": prop.created_at.isoformat() if getattr(prop, "created_at", None) else None,
    }


def resolve_property(
    landlord,
    property_query: str,
    *,
    pick: str = "",
):
    """
    Returns (property|None, error|None).

    On ambiguity without a usable pick, error includes candidates with ids so
    the model can retry with property_query=<id> or pick=first / pick=2.
    """
    from rentium.leases.models import Lease
    from rentium.properties.models import Property

    q = (property_query or "").strip()
    if not q:
        return None, "property_query is required (listing name or id)."

    # --- by primary key ---
    by_id = None
    try:
        uid = uuid.UUID(str(q))
        by_id = Property.objects.filter(landlord=landlord, pk=uid).first()
    except (ValueError, TypeError, AttributeError):
        by_id = None
    if by_id is None and str(q).isdigit():
        by_id = Property.objects.filter(landlord=landlord, pk=int(q)).first()
    if by_id is not None:
        return by_id, None

    qs = (
        Property.objects.filter(landlord=landlord, name__icontains=q)
        .select_related("group")
        .order_by("created_at", "pk")
    )
    # Prefer exact name matches when present
    exact = list(qs.filter(name__iexact=q))
    matches = exact if exact else list(qs)
    n = len(matches)
    if n == 0:
        return None, f"No listing matching {property_query!r}."
    if n == 1:
        return matches[0], None

    candidates = [_candidate_row(p) for p in matches]
    pick_s = _normalize_pick(pick)

    if pick_s in ("first", "1", "oldest"):
        return matches[0], None
    if pick_s in ("last", "newest", "latest"):
        return matches[-1], None
    if pick_s.isdigit():
        idx = int(pick_s) - 1
        if 0 <= idx < n:
            return matches[idx], None
        return None, {
            "error": f"pick={pick!r} out of range (1..{n}).",
            "candidates": candidates,
            "hint": "Pass property_query=<id> or pick=1..N / first / last / with_group / no_group.",
        }
    if pick_s in ("with_group", "grouped", "in_group"):
        grouped = [p for p in matches if p.group_id]
        if len(grouped) == 1:
            return grouped[0], None
        if len(grouped) > 1:
            return None, {
                "error": "Multiple listings with a group match pick=with_group.",
                "candidates": [_candidate_row(p) for p in grouped],
            }
        return None, {
            "error": "No matching listing is in a property group.",
            "candidates": candidates,
        }
    if pick_s in ("no_group", "ungrouped", "without_group"):
        free = [p for p in matches if not p.group_id]
        if len(free) == 1:
            return free[0], None
        if len(free) > 1:
            return None, {
                "error": "Multiple ungrouped listings match.",
                "candidates": [_candidate_row(p) for p in free],
            }
        return None, {
            "error": "No ungrouped listing matches.",
            "candidates": candidates,
        }
    if pick_s in ("with_lease", "has_lease"):
        with_l = [
            p
            for p in matches
            if Lease.objects.filter(property=p)
            .exclude(status__in=["TERMINATED", "EXPIRED"])
            .exists()
        ]
        if len(with_l) == 1:
            return with_l[0], None
        return None, {
            "error": "Could not uniquely pick with_lease.",
            "candidates": candidates,
        }
    if pick_s in ("no_lease", "without_lease"):
        no_l = [
            p
            for p in matches
            if not Lease.objects.filter(property=p)
            .exclude(status__in=["TERMINATED", "EXPIRED"])
            .exists()
        ]
        if len(no_l) == 1:
            return no_l[0], None
        return None, {
            "error": "Could not uniquely pick no_lease.",
            "candidates": candidates,
        }

    # No pick: smart default if one is clearly "the" listing
    # Prefer: in a group + has inventory; else newest with inventory; else first
    preferred = [p for p in matches if p.group_id and p.inventory_items.exists()]
    if len(preferred) == 1:
        return preferred[0], None

    return None, {
        "error": (
            f"Multiple listings match {property_query!r} ({n}). "
            "Pass property_query=<id> from candidates, or "
            "pick=oldest|newest|first|last|with_group|no_group|1|2 "
            "('the old one'→oldest, 'the new one'→newest)."
        ),
        "candidates": candidates,
        "hint": (
            "These are duplicate-named listings, NOT blocked items — pick one and "
            "the operation proceeds. 'the old one'→pick=oldest, 'the new one'→"
            "pick=newest. To rename one instead: update_property name=<new name> "
            "pick=oldest|newest."
        ),
    }


def format_resolve_error(err) -> dict | str:
    """Normalize resolve errors for tool return payloads."""
    if isinstance(err, dict):
        return err
    return {"error": err}
