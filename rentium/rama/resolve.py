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
    pick_s = (pick or "").strip().lower()

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
            "Pass property_query=<id> from candidates, or pick=first|last|with_group|no_group|1|2."
        ),
        "candidates": candidates,
        "hint": (
            "To delete a duplicate: delete_property property_query=<id> confirm=yes. "
            "Do not rename in a loop."
        ),
    }


def format_resolve_error(err) -> dict | str:
    """Normalize resolve errors for tool return payloads."""
    if isinstance(err, dict):
        return err
    return {"error": err}
