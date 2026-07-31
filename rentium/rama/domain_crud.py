"""
Landlord CRUD for RAMA — properties, leases, maintenance, inventory.

Rules (same as the dashboard / API):
- Confirm gate: no confirm=yes → needs_confirm preview only.
- Always scoped to authenticated landlord.
- Prefer model.clean() / FSM / view-level rules over inventing new logic.
- Never delete work orders (cancel via transition).
- Only DRAFT leases may be deleted; non-draft → terminate.
- ACTIVE/EXPIRED/TERMINATED/RENEWED leases are locked for field edits.
- Property delete blocked if any lease still references it (PROTECT).
- Rent shares live on LeaseTenant; total_rent is the unit rent.
"""

from __future__ import annotations

import datetime as _dt
import re
from datetime import date
from datetime import datetime
from decimal import Decimal
from decimal import InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone


def _confirmed(confirm: str) -> bool:
    return str(confirm or "").strip().lower() in ("yes", "true", "1", "y", "confirm")


def _preview(action: str, preview: dict, how: str) -> dict:
    return {
        "needs_confirm": True,
        "action": action,
        "preview": preview,
        "instruction": (
            f"Show this preview to the landlord. If they approve, call {action} "
            f"again with the same arguments AND confirm=yes. {how}"
        ),
        "ui_rules": True,
    }


def _ask_for(question: str, recall_hint: str) -> dict:
    """Deterministic 'ask the landlord for a missing ESSENTIAL value' payload.

    Reuses the same shape as playbooks._disambiguation_payload: it sets
    question_for_user + relay_instruction but NOT needs_confirm, so the persona
    (roles.py: "if a result has question_for_user, ask it VERBATIM then STOP")
    asks the question, waits for a free-form answer, and re-calls the tool with
    the value supplied — the same machinery that collects pick=oldest|newest.
    This is how RAMA follows up for essential info (rent, start date) instead of
    silently creating a broken record.
    """
    return {
        "needs_input": True,
        "question_for_user": question,
        "relay_instruction": (
            "Ask the landlord question_for_user VERBATIM, then STOP and wait. "
            "Do NOT create or preview anything yet. " + recall_hint
        ),
    }


def _truthy(val: str) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _money(value: str, default: str = "0"):
    raw = str(value if value not in (None, "") else default)
    raw = raw.replace("$", "").replace(",", "").strip()
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid money amount {value!r}") from exc


def _parse_date(value: str, field: str = "date") -> date:
    s = (value or "").strip()
    if not s:
        raise ValueError(f"{field} is required (YYYY-MM-DD).")
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid {field} {value!r}; use YYYY-MM-DD.") from exc


def _resolve_property(landlord, property_query: str, pick: str = ""):
    from .resolve import resolve_property

    return resolve_property(landlord, property_query, pick=pick)


# ---------------------------------------------------------------------------
# Blockers — the ONE source of truth for "why can't this run", shared by the
# single tools below and by plan partitioning (tool_meta / playbooks). Each
# returns [] when the action can proceed, else a list of dicts with a machine
# `reason`, a human `detail`, and optional context.
# ---------------------------------------------------------------------------


def lease_protect_blockers(prop) -> list[dict]:
    """Why this property cannot be deleted: Lease.property is DB PROTECT."""
    from rentium.leases.models import Lease

    lease_qs = Lease.objects.filter(property=prop)
    n_leases = lease_qs.count()
    if not n_leases:
        return []
    sample = list(lease_qs.values_list("lease_number", "status")[:5])
    return [
        {
            "reason": "leases_protect",
            "detail": (
                f"{n_leases} lease(s) still reference it (DB PROTECT, same as "
                "UI). Terminate/delete draft leases first."
            ),
            "leases": [{"lease_number": ln, "status": st} for ln, st in sample],
        },
    ]


def work_order_protect_blockers(prop) -> list[dict]:
    """Why this property cannot be deleted: WorkOrder.property is DB PROTECT
    and work orders are never deleted (only cancelled) — so ANY work order,
    even a completed one, keeps the listing undeletable."""
    n_wos = prop.work_orders.count()
    if not n_wos:
        return []
    open_n = prop.work_orders.exclude(status__in=["COMPLETED", "CANCELLED"]).count()
    return [
        {
            "reason": "work_orders_protect",
            "detail": (
                f"{n_wos} work order(s) reference it ({open_n} open). Work "
                "orders are permanent records (DB PROTECT, never deleted), so "
                "this listing cannot be deleted — only hidden or marked "
                "NOT_AVAILABLE."
            ),
            "open_work_orders": open_n,
        },
    ]


def property_delete_blockers(prop) -> list[dict]:
    """Everything preventing a hard delete of this listing."""
    return lease_protect_blockers(prop) + work_order_protect_blockers(prop)


def delete_property_blockers(
    landlord, *, property_query: str = "", pick: str = "", **_,
) -> list[dict]:
    """Blockers for delete_property, keyed by the same args the tool takes."""
    prop, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return [{"reason": "unresolved", "detail": str(err)}]
    return property_delete_blockers(prop)


def lease_terminate_blockers(lease) -> list[dict]:
    """Why this lease cannot be terminated (already final, or draft)."""
    from rentium.leases.models import Lease

    if lease.status in (
        Lease.LeaseStatus.TERMINATED,
        Lease.LeaseStatus.EXPIRED,
        Lease.LeaseStatus.RENEWED,
    ):
        return [
            {
                "reason": "already_final",
                "detail": f"Lease is already final ({lease.status}).",
            },
        ]
    if lease.status == Lease.LeaseStatus.DRAFT:
        return [
            {
                "reason": "draft",
                "detail": (
                    "Draft leases should be deleted with delete_draft_lease, "
                    "not terminated."
                ),
            },
        ]
    return []


def terminate_lease_blockers(
    landlord, *, property_query: str = "", lease_number: str = "", **_,
) -> list[dict]:
    """Blockers for terminate_lease, keyed by the same args the tool takes."""
    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return [{"reason": "unresolved", "detail": str(err)}]
    return lease_terminate_blockers(lease)


def delete_draft_lease_blockers(
    landlord, *, property_query: str = "", lease_number: str = "", **_,
) -> list[dict]:
    """Blockers for delete_draft_lease: only DRAFT leases are deletable."""
    from rentium.leases.models import Lease

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return [{"reason": "unresolved", "detail": str(err)}]
    if lease.status != Lease.LeaseStatus.DRAFT:
        return [
            {
                "reason": "not_draft",
                "detail": (
                    f"Only DRAFT leases can be deleted (this is {lease.status}). "
                    "Use terminate_lease instead."
                ),
            },
        ]
    return []


def _prop_err(err):
    """Return a tool error payload from resolve_property."""
    if isinstance(err, dict):
        return err if "error" in err else {"error": err}
    return {"error": err}


def _resolve_lease(
    landlord, *, property_query: str = "", lease_number: str = "", pick: str = "",
):
    from rentium.leases.models import Lease

    ln = (lease_number or "").strip()
    if ln:
        lease = (
            Lease.objects.filter(landlord=landlord, lease_number__iexact=ln)
            .select_related("property", "group")
            .first()
        )
        if not lease:
            return None, f"No lease with number {ln!r}."
        return lease, None
    pq = (property_query or "").strip()
    if not pq:
        return None, "Pass property_query or lease_number."
    prop, err = _resolve_property(landlord, pq, pick=pick)
    if err:
        return None, err
    lease = (
        Lease.objects.filter(landlord=landlord, property=prop)
        .exclude(
            status__in=[
                Lease.LeaseStatus.TERMINATED,
                Lease.LeaseStatus.EXPIRED,
            ],
        )
        .order_by("-created_at")
        .select_related("property", "group")
        .first()
    )
    if not lease:
        return None, f"No open lease on {prop.name}."
    return lease, None


def _resolve_group(landlord, group_name: str):
    from rentium.properties.models import PropertyGroup

    q = (group_name or "").strip()
    if not q:
        return None, "group_name is required."
    qs = PropertyGroup.objects.filter(landlord=landlord, name__icontains=q)
    n = qs.count()
    if n == 0:
        return None, f"No property group matching {group_name!r}."
    if n > 1:
        names = list(qs.values_list("name", flat=True)[:8])
        return None, f"Multiple groups match {group_name!r}: {names}."
    return qs.first(), None


def _resolve_holding(landlord, holding_name: str):
    from rentium.properties.models import PropertyHolding

    q = (holding_name or "").strip()
    if not q:
        return None, "holding_name is required."
    qs = PropertyHolding.objects.filter(landlord=landlord, name__icontains=q)
    n = qs.count()
    if n == 0:
        return None, f"No holding matching {holding_name!r}."
    if n > 1:
        names = list(qs.values_list("name", flat=True)[:8])
        return None, f"Multiple holdings match {holding_name!r}: {names}."
    return qs.first(), None


def _normalise_location(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _holding_for_location(landlord, address: str, holding_name: str = ""):
    """Resolve a physical holding without guessing across similar addresses."""
    from rentium.properties.models import PropertyHolding

    if (holding_name or "").strip():
        return _resolve_holding(landlord, holding_name)
    wanted = _normalise_location(address)
    if not wanted:
        return None, None
    matches = [
        holding
        for holding in PropertyHolding.objects.filter(landlord=landlord)
        if wanted
        in {
            _normalise_location(holding.address),
            _normalise_location(holding.name),
        }
    ]
    if len(matches) > 1:
        return None, (
            f"Multiple holdings match address {address!r}: "
            f"{[holding.name for holding in matches[:8]]}."
        )
    return (matches[0], None) if matches else (None, None)


def _default_lease_type(prop) -> str:
    """Mirror leases/api/views.py lease_types_view for NEW leases."""
    from rentium.leases.models import Lease
    from rentium.properties.models import Property

    if prop.property_category == Property.PropertyCategory.ROOM:
        return Lease.LeaseType.GENERIC_ROOMMATE
    prov = (prop.province or prop.province_code or "").lower()
    if prov == "bc":
        return Lease.LeaseType.BC_RESIDENTIAL_TENANCY
    if prov == "sk":
        return Lease.LeaseType.SK_RESIDENTIAL_TENANCY
    return Lease.LeaseType.GENERIC_RESIDENTIAL


def _choice_code(choices, value):
    """Map a human value to a valid choice code, tolerating case, spaces vs
    underscores, and the display label ('Garden Suite' / 'garden suite' /
    'GARDEN_SUITE' → 'GARDEN_SUITE'). Returns the code, or None if no match."""
    v = (value or "").strip()
    if not v:
        return None
    cands = {v.lower(), v.lower().replace(" ", "_"), v.lower().replace("_", " ")}
    for code, label in choices:
        if str(code).lower() in cands or str(label).lower() in cands:
            return code
    return None


def _validation_error_payload(exc: Exception) -> dict:
    if isinstance(exc, ValidationError):
        if hasattr(exc, "message_dict"):
            # Flatten the field errors INTO the message so the real reason always
            # reaches the plan report and the landlord — "Validation failed" alone
            # (with the detail hidden in a separate key) left RAMA unable to say
            # why, and guessing a different property type.
            parts = []
            for field, msgs in exc.message_dict.items():
                label = "form" if field == "__all__" else field
                parts.append(f"{label}: {'; '.join(str(m) for m in msgs)}")
            return {
                "error": "Couldn't save — " + " | ".join(parts),
                "details": exc.message_dict,
            }
        if hasattr(exc, "messages"):
            return {"error": "Couldn't save — " + "; ".join(str(m) for m in exc.messages)}
        return {"error": f"Couldn't save — {exc}"}
    return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Capabilities map (for the model + humans)
# ---------------------------------------------------------------------------


def crud_capabilities(landlord) -> dict:
    """Read-only map of CRUD the agent can perform and hard UI restrictions."""
    return {
        "properties": {
            "create": "create_property — name, address, city, ROOM|COMPLETE_UNIT",
            "update": "update_property — structured listing fields, including "
            "ROOM↔COMPLETE_UNIT classification and unit layout facts",
            "delete": "delete_property — blocked if any lease exists (PROTECT)",
            "group": "create_property_group, assign_property_to_group (rooms only)",
            "holding": "create_holding, assign_property_to_holding (any category — "
            "the house/building a bank-balance policy attaches to)",
        },
        "leases": {
            "create": "create_lease — always DRAFT; type from property category",
            "update": "update_lease — only if NOT locked (not ACTIVE/EXPIRED/…)",
            "delete": "delete_draft_lease — DRAFT only",
            "terminate": "terminate_lease — voids open charges, closes occupancy",
            "sign": "landlord_sign_lease — rent must be fully allocated",
            "tenants": "invite/add_roommate/cancel/replace/rebalance (domain_actions)",
        },
        "maintenance": {
            "create": "create_work_order",
            "update_fields": "update_work_order (title/priority/contractor…)",
            "status": "transition_work_order only (FSM)",
            "complete": "complete_work_order — optional cost + post_expense",
            "delete": "FORBIDDEN — cancel via transition instead",
            "comment": "add_work_order_comment",
        },
        "inventory": {
            "private": "create/update/delete_inventory_item on a listing",
            "shared": "create/delete_shared_inventory_item on a property group",
            "note": "is_furnished is derived from inventory (signals)",
        },
        "sets_and_chains": {
            "find": "find_listings / find_leases — deterministic set scoping; "
            "returns the COMPLETE matching set (never enumerate yourself)",
            "plan": "plan_operation — bulk/multi-step plans over listings "
            "(delete_listings, terminate_and_delete, update_status)",
            "move": "plan_move_tenant — end current lease, re-lease another room",
            "confirm": "one 'yes' runs a whole plan; lease terminations always "
            "pause for their own confirmation (server-side policy)",
        },
        "confirm_rule": "Every mutating tool: preview without confirm, then confirm=yes",
    }


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def _parse_item_names(inventory_items: str) -> list[str]:
    """Comma-separated or JSON list of furniture names."""
    raw = (inventory_items or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            import json

            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:  # noqa: BLE001
            pass
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


def _group_property_consensus(group) -> tuple[dict | None, dict | None]:
    """Derive room-level location/container fields from a group's members."""
    members = list(
        group.grouped_properties.select_related("holding").order_by("created_at", "pk"),
    )
    if not members:
        return None, {
            "needs_input": True,
            "question_for_user": (
                f"{group.name} has no rooms to derive an address and holding from. "
                "Add an existing room to the group first."
            ),
            "missing_group_data": ["members"],
        }

    def agreed(field: str, *, required: bool = False):
        raw = [getattr(member, field) for member in members]
        normalised = {
            str(value).strip().casefold()
            for value in raw
            if value is not None and str(value).strip()
        }
        missing = any(value is None or not str(value).strip() for value in raw)
        # A partial value is not consensus: copying it would silently make the
        # new room more specific than some of its siblings. Optional fields may
        # be blank only when every member leaves them blank.
        if len(normalised) > 1 or (missing and (required or bool(normalised))):
            return None, {
                "field": field,
                "values": [
                    {
                        "room": member.name,
                        "value": getattr(member, field) or None,
                    }
                    for member in members
                ],
                "reason": "inconsistent" if len(normalised) > 1 else "missing",
            }
        if not normalised:
            return "", None
        return next(
            value
            for value in raw
            if value is not None and str(value).strip()
        ), None

    derived: dict = {}
    problems: list[dict] = []
    for field, required in (
        ("address", True),
        ("city", True),
        ("province", True),
        ("postal_code", False),
        ("country", False),
    ):
        value, problem = agreed(field, required=required)
        if problem:
            problems.append(problem)
        else:
            derived[field] = value

    holding_ids = {member.holding_id for member in members}
    if len(holding_ids) != 1 or None in holding_ids:
        problems.append(
            {
                "field": "holding",
                "reason": "missing" if holding_ids == {None} else "inconsistent",
                "values": [
                    {
                        "room": member.name,
                        "value": member.holding.name if member.holding_id else None,
                    }
                    for member in members
                ],
            },
        )
    else:
        derived["holding"] = members[0].holding

    verified_values = {bool(member.address_verified) for member in members}
    if len(verified_values) == 1:
        derived["address_verified"] = verified_values.pop()
    else:
        problems.append(
            {
                "field": "address_verified",
                "reason": "inconsistent",
                "values": [
                    {
                        "room": member.name,
                        "value": bool(member.address_verified),
                    }
                    for member in members
                ],
            },
        )
    if problems:
        fields = ", ".join(problem["field"] for problem in problems)
        return None, {
            "needs_input": True,
            "question_for_user": (
                f"I can't safely derive the new room's property data from "
                f"{group.name}: {fields} is missing or inconsistent across its "
                "rooms. Correct the group members first, then ask me again."
            ),
            "group_data_problems": problems,
        }
    return derived, None


def _group_room_property_data(
    landlord,
    group,
    *,
    address: str = "",
    city: str = "",
    province: str = "",
    holding_name: str = "",
) -> tuple[dict | None, dict | None]:
    """Derive group-room location, with an explicit empty-group bootstrap.

    Existing member consensus always wins. An empty group may be initialized
    from a uniquely resolved holding plus explicit missing location fields; the
    preview exposes those values before anything is written.
    """
    members_exist = group.grouped_properties.exists()
    if members_exist:
        derived, error = _group_property_consensus(group)
        if error:
            return derived, error
        supplied = {
            "address": (address or "").strip(),
            "city": (city or "").strip(),
            "province": (province or "").strip(),
        }
        conflicts = [
            field
            for field, value in supplied.items()
            if value
            and _normalise_location(value)
            != _normalise_location(str(derived.get(field) or ""))
        ]
        if holding_name.strip():
            holding, holding_error = _resolve_holding(landlord, holding_name)
            if holding_error:
                return None, {"error": holding_error}
            if holding.pk != derived["holding"].pk:
                conflicts.append("holding")
        if conflicts:
            return None, {
                "needs_input": True,
                "question_for_user": (
                    f"The supplied {', '.join(conflicts)} conflicts with the "
                    f"recorded rooms in {group.name}. Correct the group or omit "
                    "the conflicting bootstrap values."
                ),
                "group_data_problems": conflicts,
            }
        return derived, None

    holding, holding_error = _holding_for_location(
        landlord,
        address,
        holding_name,
    )
    if holding_error:
        return None, {"error": holding_error}
    if holding is None:
        return None, {
            "needs_input": True,
            "question_for_user": (
                f"{group.name} is empty. Which existing physical holding should "
                "this group use? Give its holding name or exact street address."
            ),
            "missing_group_data": ["holding"],
        }

    canonical_address = (holding.address or address or "").strip()
    canonical_city = (holding.city or city or "").strip()
    if (
        address.strip()
        and holding.address.strip()
        and _normalise_location(address) != _normalise_location(holding.address)
    ):
        return None, {
            "needs_input": True,
            "question_for_user": (
                f"{holding.name} is recorded at {holding.address}, not {address}. "
                "Correct the holding or choose the right one."
            ),
            "group_data_problems": ["address"],
        }
    if (
        city.strip()
        and holding.city.strip()
        and _normalise_location(city) != _normalise_location(holding.city)
    ):
        return None, {
            "needs_input": True,
            "question_for_user": (
                f"{holding.name} is recorded in {holding.city}, not {city}. "
                "Correct the holding or choose the right one."
            ),
            "group_data_problems": ["city"],
        }

    sibling_rows = list(holding.listings.all())

    def sibling_consensus(field: str) -> str:
        values = {
            str(getattr(item, field) or "").strip()
            for item in sibling_rows
            if str(getattr(item, field) or "").strip()
        }
        return values.pop() if len(values) == 1 else ""

    raw_province = (province or sibling_consensus("province")).strip()
    if raw_province:
        from rentium.properties.models import Province
        from rentium.properties.models import normalise_province

        canonical_province = (
            normalise_province(raw_province) or raw_province.casefold()[:2]
        )
        if canonical_province not in Province.values:
            return None, {
                "error": (
                    f"Invalid province {raw_province!r}. Use a two-letter "
                    "Canadian province code."
                ),
            }
    else:
        canonical_province = ""
    missing = [
        field
        for field, value in (
            ("address", canonical_address),
            ("city", canonical_city),
            ("province", canonical_province),
        )
        if not value
    ]
    if missing:
        return None, {
            "needs_input": True,
            "question_for_user": (
                f"{group.name} can use holding {holding.name}, but "
                f"{', '.join(missing)} is not recorded. Provide it before I "
                "prepare the room preview."
            ),
            "missing_group_data": missing,
        }
    return {
        "address": canonical_address,
        "city": canonical_city,
        "province": canonical_province,
        "postal_code": sibling_consensus("postal_code"),
        "country": sibling_consensus("country") or "Canada",
        "holding": holding,
        "address_verified": False,
    }, None


def create_property(
    landlord,
    *,
    name: str,
    address: str,
    city: str,
    property_category: str = "ROOM",
    province: str = "bc",
    status: str = "AVAILABLE",
    unit_type: str = "",
    room_type: str = "PRIVATE",
    bedrooms: str = "",
    bathrooms: str = "",
    description: str = "",
    group_name: str = "",
    asking_rent: str = "",
    inventory_items: str = "",
    allow_duplicate_name: str = "0",
    confirm: str = "",
) -> dict:
    """Create listing. If inventory_items is set (e.g. 'Single bed, Mattress'),
    private inventory rows are created in the same confirm step so 'What's in it'
    is not empty after the user mentioned furniture.
    Rejects exact-name duplicates unless allow_duplicate_name=yes."""
    from rentium.properties.models import InventoryItem
    from rentium.properties.models import Property
    from rentium.properties.models import Province
    from rentium.properties.services import listing_name_conflicts

    name = (name or "").strip()
    address = (address or "").strip()
    city = (city or "").strip()
    if not name or not address or not city:
        return {"error": "name, address, and city are required."}

    # Prevent silent duplicate names (Room G × 2 broke lease/delete tools)
    from .resolve import _candidate_row

    dup_qs = Property.objects.filter(landlord=landlord, name__iexact=name).order_by(
        "created_at",
    )
    if dup_qs.exists() and not _truthy(allow_duplicate_name):
        # Unlike groups, listings are NOT idempotent by name alone — two real
        # listings can share a name, and letting that through is what broke the
        # lease and delete tools. But a same-named listing at the SAME address
        # of the SAME category is a restatement of one the model just made, not
        # a second thing. Treat that exact match as already-done rather than
        # sending the model off to rename or delete something.
        wanted_cat = (property_category or "ROOM").strip().upper()
        same = [
            p
            for p in dup_qs
            if (p.address or "").strip().lower() == address.lower()
            and (p.city or "").strip().lower() == city.lower()
            and (p.property_category or "").upper() == wanted_cat
        ]
        if len(same) == 1:
            existing = same[0]
            return {
                "created": False,
                "reused": True,
                "property": _candidate_row(existing),
                "note": (
                    f"{existing.name!r} already exists at this address — "
                    "reusing it, nothing changed."
                ),
            }
        return {
            "error": (
                f"You already have a listing named {name!r}. "
                "Do not create another with the same name."
            ),
            "candidates": [_candidate_row(p) for p in dup_qs[:10]],
            "hint": (
                "Reuse property_query=<id>, rename the old one, or delete the "
                "duplicate with delete_property property_query=<id> pick=…, "
                "then continue lease/invite/inspection on the remaining listing."
            ),
        }
    name_conflicts = [
        row
        for row in listing_name_conflicts(landlord, name)
        if row["match"] == "near"
    ]

    cat = _choice_code(Property.PropertyCategory.choices, property_category or "ROOM")
    if cat is None:
        return {
            "error": "property_category must be one of "
            f"{[l for _, l in Property.PropertyCategory.choices]}",
        }

    st = _choice_code(Property.PropertyStatus.choices, status or "AVAILABLE") \
        or Property.PropertyStatus.AVAILABLE

    prov_raw = (province or "bc").strip().lower()
    from rentium.properties.models import normalise_province

    prov = normalise_province(prov_raw) or prov_raw[:2]
    if prov not in Province.values:
        return {
            "error": f"Invalid province {province!r}. Use BC, ON, AB, … (two-letter).",
        }

    ut = _choice_code(Property.UnitType.choices, unit_type)
    if unit_type.strip() and ut is None:
        return {
            "error": "unit_type must be one of: "
            + ", ".join(str(l) for _, l in Property.UnitType.choices),
        }
    rt = _choice_code(Property.RoomType.choices, room_type)
    if room_type.strip() and rt is None:
        return {
            "error": "room_type must be one of: "
            + ", ".join(str(l) for _, l in Property.RoomType.choices),
        }
    # Safety net for a mis-classified category. Unambiguous "complete home"
    # phrases in the name (or an explicit unit_type) mean COMPLETE_UNIT — so a
    # "garden suite" is never silently downgraded to a Private Room even when the
    # model defaulted property_category=ROOM. (room_type can't be trusted here: the
    # wrapper always sends a default, so its presence isn't a real ROOM signal.)
    name_l = name.lower()
    _name_unit_type = {
        "garden suite": Property.UnitType.GARDEN_SUITE,
        "basement suite": Property.UnitType.BASEMENT,
        "basement unit": Property.UnitType.BASEMENT,
        "main floor": Property.UnitType.MAIN_FLOOR,
        "apartment": Property.UnitType.APARTMENT,
        "laneway": Property.UnitType.OTHER,
        "in-law suite": Property.UnitType.OTHER,
        "secondary suite": Property.UnitType.OTHER,
    }
    inferred_ut = next((v for k, v in _name_unit_type.items() if k in name_l), None)
    if (ut is not None or inferred_ut) and cat == Property.PropertyCategory.ROOM:
        cat = Property.PropertyCategory.COMPLETE_UNIT
        ut = ut or inferred_ut
    if cat == Property.PropertyCategory.COMPLETE_UNIT:
        ut = ut or Property.UnitType.OTHER
        rt = None
    else:
        rt = rt or Property.RoomType.PRIVATE
        ut = None

    group = None
    if group_name.strip():
        if cat != Property.PropertyCategory.ROOM:
            return {"error": "Only ROOM listings can join a property group."}
        group, gerr = _resolve_group(landlord, group_name)
        if gerr:
            return {"error": gerr}
    holding, holding_error = _holding_for_location(landlord, address)
    if holding_error:
        return {"error": holding_error}
    if holding is not None:
        # The exact-address holding is the canonical physical record. Model
        # arguments copied from old prose must not create a listing whose city
        # disagrees with its own holding.
        address = (holding.address or address).strip()
        city = (holding.city or city).strip()
    if group is not None and group.grouped_properties.exists():
        group_data, group_error = _group_property_consensus(group)
        if group_error is None:
            for field, supplied in (
                ("address", address),
                ("city", city),
                ("province", prov),
            ):
                if _normalise_location(supplied) != _normalise_location(
                    str(group_data.get(field) or ""),
                ):
                    return {
                        "error": (
                            f"{field}={supplied!r} conflicts with the recorded "
                            f"{group.name} value {group_data.get(field)!r}."
                        ),
                    }
            holding = group_data["holding"]

    beds = int(bedrooms) if str(bedrooms).strip().isdigit() else None
    baths = None
    if str(bathrooms).strip():
        try:
            baths = Decimal(str(bathrooms))
        except (InvalidOperation, ValueError):
            return {"error": f"Invalid bathrooms {bathrooms!r}."}

    ask = None
    if str(asking_rent).strip():
        try:
            ask = _money(asking_rent)
        except ValueError as exc:
            return {"error": str(exc)}

    inv_names = _parse_item_names(inventory_items)

    preview = {
        "name": name,
        "address": address,
        "city": city,
        "province": prov,
        "property_category": cat,
        "unit_type": ut,
        "room_type": rt,
        "status": st,
        "group": group.name if group else None,
        "holding": holding.name if holding else None,
        "asking_rent": str(ask) if ask is not None else None,
        "description": (description or "")[:200],
        "inventory_items_to_create": inv_names,
        "name_conflicts": name_conflicts,
        "duplicate_warning": (
            "A similarly named listing already exists. Confirm only if this is "
            "a genuinely separate room."
            if name_conflicts
            else None
        ),
        "note": (
            "Furniture listed here becomes private inventory (What's in it / "
            "prints on roommate agreement + condition inspection)."
            if inv_names
            else "No inventory_items passed — 'What's in it' will stay empty until added."
        ),
    }
    if not _confirmed(confirm):
        return _preview(
            "create_property",
            preview,
            "Creates a listing owned by this landlord (same rules as Properties UI). "
            "Pass inventory_items when the landlord names furniture.",
        )

    prop = Property(
        landlord=landlord,
        name=name[:255],
        address=address[:255],
        city=city[:100],
        province=prov,
        status=st,
        property_category=cat,
        unit_type=ut,
        room_type=rt,
        bedrooms=beds,
        bathrooms=baths,
        description=(description or "")[:5000],
        group=group,
        holding=holding,
        asking_rent=ask,
        country="Canada",
    )
    try:
        prop.full_clean()
        prop.save()
    except ValidationError as exc:
        return _validation_error_payload(exc)

    created_items = []
    for iname in inv_names:
        item = InventoryItem.objects.create(
            property=prop,
            name=iname[:200],
            quantity=1,
            condition=InventoryItem.ItemCondition.GOOD,
        )
        created_items.append({"id": str(item.pk), "name": item.name})
    prop.refresh_from_db()

    return {
        "created": True,
        "message": (
            f"Created {prop.name} in {prop.group.name}."
            if prop.group_id
            else f"Created {prop.name}."
        ),
        "property": {
            "id": str(prop.pk),
            "name": prop.name,
            "category": prop.property_category,
            "type_display": (
                prop.get_room_type_display()
                if prop.property_category == Property.PropertyCategory.ROOM
                else prop.get_unit_type_display()
            ),
            "address": prop.address,
            "city": prop.city,
            "province": prop.province,
            "status": prop.status,
            "group": prop.group.name if prop.group_id else None,
            "is_furnished": bool(prop.is_furnished),
            "inventory_created": created_items,
        },
        "next_steps": [
            "Add photos in the UI (required for public enquiries)",
            "Create DRAFT lease with create_lease (security deposit defaults to half month if omitted)",
            "Invite tenant, then create_condition_inspection for move-in day",
        ],
    }


def _copy_image_field(src_field, dst_saver) -> bool:
    """Read a source ImageField's bytes and save an independent copy via
    dst_saver(basename, ContentFile). Returns True on success."""
    import os

    from django.core.files.base import ContentFile

    if not src_field:
        return False
    try:
        src_field.open("rb")
        data = src_field.read()
        src_field.close()
        dst_saver(os.path.basename(src_field.name), ContentFile(data))
        return True
    except Exception:  # noqa: BLE001 — a broken source file shouldn't abort the copy
        return False


def duplicate_listing(
    landlord,
    *,
    property_query: str,
    new_name: str = "",
    copy_images: str = "1",
    copy_inventory: str = "1",
    group_name: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Duplicate an existing listing — a real copy, WITH its photos and inventory
    (this is what a landlord means by 'duplicate this listing', not a blank new
    one). Copies the source's address, city, category, room/unit type, beds,
    description, asking rent, status, and — unless turned off — its primary photo
    + gallery images and its private inventory rows. new_name defaults to the
    same name (it's a deliberate duplicate). Leases are NOT copied. Preview;
    confirm=yes."""
    from rentium.properties.models import InventoryItem
    from rentium.properties.models import Property
    from rentium.properties.models import PropertyImage

    src, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return _prop_err(err)

    name = (new_name or "").strip() or src.name
    do_images = _truthy(copy_images)
    do_inventory = _truthy(copy_inventory)
    n_gallery = src.property_images.count()
    n_images = n_gallery + (1 if src.primary_image else 0)
    n_inventory = src.inventory_items.count()

    group = None
    if group_name.strip():
        group, gerr = _resolve_group(landlord, group_name)
        if gerr:
            return {"error": gerr}
    elif src.group_id:
        group = src.group

    preview = {
        "source": src.name,
        "source_id": str(src.pk),
        "new_name": name,
        "will_copy": {
            "images": n_images if do_images else 0,
            "inventory_items": n_inventory if do_inventory else 0,
            "address_city": f"{src.address}, {src.city}",
            "category": src.property_category,
            "asking_rent": str(src.asking_rent) if src.asking_rent else None,
            "group": group.name if group else None,
        },
        "note": "Leases are never copied — the duplicate starts with no tenancy.",
    }
    if not _confirmed(confirm):
        return _preview(
            "duplicate_listing",
            preview,
            "Copies the listing WITH its photos and inventory. confirm=yes to run.",
        )

    dup = Property(
        landlord=landlord,
        name=name[:255],
        address=src.address,
        city=src.city,
        province=src.province,
        property_category=src.property_category,
        room_type=src.room_type,
        unit_type=src.unit_type,
        bedrooms=src.bedrooms,
        bathrooms=src.bathrooms,
        description=src.description,
        asking_rent=src.asking_rent,
        status=src.status,
        group=group,
        is_publicly_visible=src.is_publicly_visible,
    )
    try:
        dup.save()  # duplicate names are allowed here on purpose (it's a copy)
    except ValidationError as exc:
        return _validation_error_payload(exc)

    copied_images = 0
    if do_images:
        if src.primary_image and _copy_image_field(
            src.primary_image, lambda n, c: dup.primary_image.save(n, c, save=True),
        ):
            copied_images += 1
        for img in src.property_images.all().order_by("order", "created_at"):
            new_img = PropertyImage(property=dup, caption=img.caption, order=img.order)
            if _copy_image_field(
                img.image, lambda n, c: new_img.image.save(n, c, save=True),
            ):
                copied_images += 1

    copied_inventory = 0
    if do_inventory:
        for it in src.inventory_items.all():
            InventoryItem.objects.create(
                property=dup,
                name=it.name,
                description=it.description,
                quantity=it.quantity,
                condition=it.condition,
                location_description=it.location_description,
            )
            copied_inventory += 1

    return {
        "created": True,
        "duplicated_from": src.name,
        "listing": {
            "id": str(dup.pk),
            "name": dup.name,
            "status": dup.status,
            "group": group.name if group else None,
        },
        "copied_images": copied_images,
        "copied_inventory": copied_inventory,
        "note": (
            f"Duplicated {src.name} → {dup.name} with {copied_images} photo(s) and "
            f"{copied_inventory} inventory item(s). No lease was copied."
        ),
    }


def attach_photo_to_listing(
    landlord,
    *,
    property_query: str,
    attachment_batch_id: str = "",
    attachment_ids: str = "",
    upload_id: str = "",
    set_primary: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Attach only the explicitly named batch (or explicit legacy upload IDs).

    The old blank/``all`` fallback selected every unused photo in the landlord's
    account.  That made one 11-photo message consume 28 files from unrelated
    conversations.  A batch is now the unit of intent and ordering.
    """
    import os
    from pathlib import Path

    from django.core.files.base import ContentFile

    from rentium.properties.media_services import attach_media
    from rentium.properties.models import PropertyImage
    from rentium.rama.attachment_services import resolve_batch_attachments
    from rentium.rama.models import RamaAttachment
    from rentium.rama.models import RamaUpload

    prop, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return _prop_err(err)

    batch_id = (attachment_batch_id or "").strip()
    requested_ids = [
        value.strip()
        for value in (attachment_ids or "").replace(";", ",").split(",")
        if value.strip()
    ]
    if batch_id:
        batch, attachments = resolve_batch_attachments(
            landlord=landlord,
            batch_id=batch_id,
            attachment_ids=requested_ids or None,
        )
        if batch is None:
            return {"error": "That attachment batch does not exist in this portfolio."}
        if requested_ids and len(attachments) != len(set(requested_ids)):
            return {
                "error": (
                    "One or more selected attachments are not available in that batch. "
                    "No photos were changed."
                ),
            }
        image_suffixes = {
            ".jpg",
            ".jpeg",
            ".png",
            ".tif",
            ".tiff",
            ".webp",
            ".heic",
            ".heif",
        }
        image_attachments = [
            row
            for row in attachments
            if Path(row.original_filename).suffix.casefold() in image_suffixes
        ]
        if len(image_attachments) != len(attachments):
            rejected = [
                row.original_filename
                for row in attachments
                if row not in image_attachments
            ]
            return {
                "error": (
                    "The selected batch includes non-image files, so nothing was "
                    "attached. Select only the property photos."
                ),
                "non_images": rejected,
            }
        if not image_attachments:
            already = list(
                batch.attachments.filter(
                    status=RamaAttachment.Status.APPLIED,
                    target_type="property",
                    target_id=str(prop.pk),
                ).values_list("pk", flat=True),
            )
            if already:
                return {
                    "already_done": (
                        f"This batch was already attached to {prop.name}; no action "
                        "was repeated."
                    ),
                    "attachment_ids": [str(pk) for pk in already],
                }
            return {"error": "That batch has no available property photos."}

        make_primary = _truthy(set_primary)
        if not _confirmed(confirm):
            return _preview(
                "attach_photo_to_listing",
                {
                    "listing": prop.name,
                    "listing_id": str(prop.pk),
                    "attachment_batch_id": str(batch.pk),
                    "attachment_ids": [str(row.pk) for row in image_attachments],
                    "filenames": [row.original_filename for row in image_attachments],
                    "photos": len(image_attachments),
                    "first_as": "primary photo" if make_primary else "gallery photo",
                    "rest_as": "gallery photos" if len(image_attachments) > 1 else None,
                },
                (
                    f"Adds exactly {len(image_attachments)} photo(s) from attachment "
                    f"batch {batch.pk} to the listing. confirm=yes to apply."
                ),
            )

        handles = attach_media(
            property_obj=prop,
            sources=[row.original for row in image_attachments],
            make_first_primary=make_primary,
        )
        for row, handle in zip(image_attachments, handles, strict=True):
            row.classification = RamaAttachment.Classification.PROPERTY_PHOTO
            row.status = RamaAttachment.Status.APPLIED
            row.target_type = "property"
            row.target_id = str(prop.pk)
            row.result = handle
            row.save(
                update_fields=[
                    "classification",
                    "status",
                    "target_type",
                    "target_id",
                    "result",
                    "updated_at",
                ],
            )
        remaining = batch.attachments.filter(
            status__in=[
                RamaAttachment.Status.STAGED,
                RamaAttachment.Status.CLASSIFIED,
            ],
        ).exists()
        if not remaining:
            from rentium.rama.models import RamaAttachmentBatch

            batch.status = RamaAttachmentBatch.Status.COMPLETED
            batch.completed_at = timezone.now()
            batch.save(update_fields=["status", "completed_at"])
        prop.refresh_from_db()
        return {
            "attached": True,
            "listing": prop.name,
            "listing_id": str(prop.pk),
            "attachment_batch_id": str(batch.pk),
            "photos_added": len(handles),
            "media": handles,
            "image_count": prop.image_count,
            "note": f"Added {len(handles)} photo(s) to {prop.name}.",
        }

    # Legacy clients may still send upload IDs, but they must be explicit.  A
    # blank value or the old magic word "all" is intentionally rejected.
    uid = (upload_id or "").strip()
    if not uid or uid.casefold() == "all":
        return {
            "error": (
                "An explicit attachment_batch_id is required. RAMA will not use "
                "unrelated pending uploads from other messages."
            ),
        }
    qs = RamaUpload.objects.filter(landlord=landlord, used_at__isnull=True).order_by(
        "created_at",
    )
    ids = [x.strip() for x in uid.replace(";", ",").split(",") if x.strip()]
    uploads = list(qs.filter(pk__in=ids))
    if not uploads:
        return {
            "error": (
                "No attached photo to add. The landlord needs to attach a photo "
                "in the chat first (the paperclip, or send it to the Telegram bot)."
            ),
        }

    make_primary = _truthy(set_primary)
    if not _confirmed(confirm):
        return _preview(
            "attach_photo_to_listing",
            {
                "listing": prop.name,
                "listing_id": str(prop.pk),
                "photos": len(uploads),
                "first_as": "primary photo" if make_primary else "gallery photo",
                "rest_as": "gallery photos" if len(uploads) > 1 else None,
            },
            f"Adds {len(uploads)} photo(s) to the listing. confirm=yes to apply.",
        )

    added = 0
    for i, upload in enumerate(uploads):
        try:
            upload.image.open("rb")
            data = upload.image.read()
            upload.image.close()
            basename = os.path.basename(upload.image.name)
        except Exception:  # noqa: BLE001 — skip a broken upload, keep the rest
            continue
        if make_primary and i == 0:
            prop.primary_image.save(basename, ContentFile(data), save=True)
        else:
            PropertyImage(property=prop).image.save(basename, ContentFile(data), save=True)
        upload.used_at = timezone.now()
        upload.save(update_fields=["used_at"])
        added += 1

    return {
        "attached": True,
        "listing": prop.name,
        "photos_added": added,
        "image_count": prop.image_count,
        "note": f"Added {added} photo(s) to {prop.name}.",
    }


def list_listing_media(
    landlord,
    *,
    property_query: str,
    pick: str = "",
) -> dict:
    """List stable media handles so a later remove targets an exact image."""
    from rentium.properties.media_services import media_manifest

    prop, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return _prop_err(err)
    media = media_manifest(prop)
    return {
        "listing": prop.name,
        "listing_id": str(prop.pk),
        "media": media,
        "selection_help": (
            "Choose the numbered photo(s) to remove. No image changes until "
            "the exact selection is previewed and confirmed."
        ),
        "_attachment": {
            "kind": "property_media",
            "property_id": str(prop.pk),
            "label": prop.name,
            "media": media,
        },
    }


def remove_photo_from_listing(
    landlord,
    *,
    property_query: str,
    media_handle: str,
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Remove one exact listing image by the handle returned by the manifest."""
    from rentium.properties.media_services import media_manifest
    from rentium.properties.media_services import remove_media

    prop, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return _prop_err(err)
    handle = (media_handle or "").strip()
    media = {row["handle"]: row for row in media_manifest(prop)}
    target = media.get(handle)
    if target is None:
        return {
            "error": (
                f"{prop.name} has no media item {handle!r}. "
                "Call list_listing_media to get current handles."
            ),
        }
    if not _confirmed(confirm):
        return _preview(
            "remove_photo_from_listing",
            {
                "listing": prop.name,
                "listing_id": str(prop.pk),
                "media": target,
            },
            "Removes this exact listing image. The operation is audited.",
        )
    removed = remove_media(property_obj=prop, handle=handle)
    return {
        "removed": True,
        "listing": prop.name,
        "listing_id": str(prop.pk),
        "media_handle": handle,
        "effect": removed,
        "remaining_media": media_manifest(prop),
        "note": f"Removed {handle} from {prop.name}.",
    }


def remove_photos_from_listing(
    landlord,
    *,
    property_query: str,
    media_handles_json: str,
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Remove several specifically selected listing images in one operation."""
    import json

    from rentium.properties.media_services import media_manifest
    from rentium.properties.media_services import remove_media_many

    prop, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return _prop_err(err)
    try:
        raw_handles = json.loads(media_handles_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return {"error": "media_handles_json must be a JSON list of media handles."}
    if not isinstance(raw_handles, list) or any(
        not isinstance(handle, str) for handle in raw_handles
    ):
        return {"error": "media_handles_json must be a JSON list of media handles."}
    handles = list(
        dict.fromkeys(handle.strip() for handle in raw_handles if handle.strip()),
    )
    current = {row["handle"]: row for row in media_manifest(prop)}
    if not handles:
        return {
            "error": (
                "Select at least one exact image. Call list_listing_media to see "
                "the numbered gallery."
            ),
        }
    missing = [handle for handle in handles if handle not in current]
    if missing:
        return {
            "error": (
                f"{prop.name} does not have: {', '.join(missing)}. "
                "No images were changed."
            ),
        }
    selected = [current[handle] for handle in handles]
    if not _confirmed(confirm):
        return _preview(
            "remove_photos_from_listing",
            {
                "listing": prop.name,
                "listing_id": str(prop.pk),
                "media": selected,
                "count": len(selected),
            },
            "Removes exactly the selected listing images in one audited operation.",
        )
    removed = remove_media_many(property_obj=prop, handles=handles)
    return {
        "removed": True,
        "listing": prop.name,
        "listing_id": str(prop.pk),
        "media_handles": handles,
        "effects": removed,
        "remaining_media": media_manifest(prop),
        "note": f"Removed {len(removed)} selected photo(s) from {prop.name}.",
    }


def update_property(
    landlord,
    *,
    property_query: str,
    name: str = "",
    status: str = "",
    description: str = "",
    address: str = "",
    city: str = "",
    province: str = "",
    asking_rent: str = "",
    property_category: str = "",
    unit_type: str = "",
    room_type: str = "",
    bedrooms: str = "",
    bathrooms: str = "",
    max_occupancy: str = "",
    square_footage: str = "",
    is_publicly_visible: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    from rentium.properties.models import Property
    from rentium.properties.models import Province
    from rentium.properties.models import normalise_province
    from rentium.properties.services import listing_name_conflicts

    prop, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return _prop_err(err)

    changes: dict = {}
    rename_conflicts: list[dict] = []
    if name.strip():
        new_name = name.strip()[:255]
        if new_name != prop.name:
            rename_conflicts = listing_name_conflicts(
                landlord, new_name, exclude_id=prop.pk,
            )
            exact_conflicts = [
                conflict
                for conflict in rename_conflicts
                if conflict["match"] == "exact"
            ]
            if exact_conflicts:
                return {
                    "error": (
                        f"Another listing is already named {new_name!r}. "
                        "Choose a distinct name instead of creating an ambiguous duplicate."
                    ),
                    "name_conflicts": exact_conflicts,
                }
            changes["name"] = new_name
    if status.strip():
        st = _choice_code(Property.PropertyStatus.choices, status)
        if st is None:
            return {
                "error": "status must be one of: "
                + ", ".join(str(l) for _, l in Property.PropertyStatus.choices),
            }
        changes["status"] = st
    if description != "":
        changes["description"] = description[:5000]
    if address.strip():
        changes["address"] = address.strip()[:255]
    if city.strip():
        changes["city"] = city.strip()[:100]
    if province.strip():
        prov = normalise_province(province) or province.strip().lower()[:2]
        if prov not in Province.values:
            return {"error": f"Invalid province {province!r}."}
        changes["province"] = prov
    target_category = prop.property_category
    if property_category.strip():
        category_input = property_category.strip().lower().replace("_", " ")
        if category_input in {"full unit", "full suite", "entire unit", "suite"}:
            target_category = Property.PropertyCategory.COMPLETE_UNIT
        else:
            target_category = _choice_code(
                Property.PropertyCategory.choices, property_category,
            )
        if target_category is None:
            return {
                "error": "property_category must be one of: "
                + ", ".join(
                    str(label) for _, label in Property.PropertyCategory.choices
                ),
            }
        if target_category != prop.property_category and prop.leases.exists():
            return {
                "error": (
                    f"Cannot reclassify {prop.name} while lease records reference it. "
                    "Its room/unit classification affects the legal agreement. "
                    "Correct the lease history first or create a new listing."
                ),
            }
        changes["property_category"] = target_category

    if unit_type.strip():
        ut = _choice_code(Property.UnitType.choices, unit_type)
        if ut is None:
            return {
                "error": "unit_type must be one of: "
                + ", ".join(str(l) for _, l in Property.UnitType.choices),
            }
        if target_category != Property.PropertyCategory.COMPLETE_UNIT:
            return {"error": "unit_type only applies to COMPLETE_UNIT listings."}
        changes["unit_type"] = ut
    if room_type.strip():
        rt = _choice_code(Property.RoomType.choices, room_type)
        if rt is None:
            return {
                "error": "room_type must be one of: "
                + ", ".join(str(l) for _, l in Property.RoomType.choices),
            }
        if target_category != Property.PropertyCategory.ROOM:
            return {"error": "room_type only applies to ROOM listings."}
        changes["room_type"] = rt

    if target_category == Property.PropertyCategory.COMPLETE_UNIT:
        changes.setdefault(
            "unit_type",
            prop.unit_type
            or (
                Property.UnitType.GARDEN_SUITE
                if "garden suite" in prop.name.lower()
                else Property.UnitType.OTHER
            ),
        )
        if prop.property_category != target_category:
            changes.update({"room_type": None, "group": None})
        numeric_fields = {
            "bedrooms": (bedrooms, int),
            "bathrooms": (bathrooms, Decimal),
            "max_occupancy": (max_occupancy, int),
            "square_footage": (square_footage, int),
        }
        for field, (raw, caster) in numeric_fields.items():
            if raw == "":
                continue
            try:
                value = None if not raw.strip() else caster(raw)
            except (ValueError, InvalidOperation):
                return {"error": f"{field} must be a number or blank."}
            if value is not None and value < 0:
                return {"error": f"{field} cannot be negative."}
            changes[field] = value
    else:
        if any(v != "" for v in (bedrooms, bathrooms, max_occupancy, square_footage)):
            return {
                "error": "bedrooms/bathrooms/max_occupancy/square_footage "
                "only apply to COMPLETE_UNIT listings.",
            }
        if prop.property_category != target_category:
            changes.update(
                {
                    "unit_type": None,
                    "bedrooms": None,
                    "bathrooms": None,
                    "max_occupancy": None,
                    "square_footage": None,
                    "building_amenities": [],
                    "room_type": _choice_code(
                        Property.RoomType.choices, room_type or "PRIVATE",
                    ),
                },
            )
    if asking_rent != "":
        if asking_rent.strip() == "":
            changes["asking_rent"] = None
        else:
            try:
                changes["asking_rent"] = _money(asking_rent)
            except ValueError as exc:
                return {"error": str(exc)}
    if is_publicly_visible != "":
        changes["is_publicly_visible"] = _truthy(is_publicly_visible)

    requested_a_field = any(
        value != ""
        for value in (
            name,
            status,
            description,
            address,
            city,
            province,
            asking_rent,
            property_category,
            unit_type,
            room_type,
            bedrooms,
            bathrooms,
            max_occupancy,
            square_footage,
            is_publicly_visible,
        )
    )
    if not changes and requested_a_field:
        return {
            "unchanged": True,
            "property": prop.name,
            "message": f"No change needed — {prop.name} already has those values.",
        }
    if not changes:
        return {
            "error": "No fields to update. Pass a structured field; never change "
            "description as a substitute for property type or layout.",
        }

    preview = {
        "property": prop.name,
        "id": str(prop.pk),
        "changes": {
            k: (str(v) if isinstance(v, Decimal) else v) for k, v in changes.items()
        },
        "name_conflicts": rename_conflicts,
        "duplicate_warning": (
            "The new name is similar to an existing listing."
            if rename_conflicts
            else None
        ),
    }
    if not _confirmed(confirm):
        return _preview("update_property", preview, "Updates listing fields.")

    previous_name = prop.name
    for k, v in changes.items():
        setattr(prop, k, v)
    try:
        prop.full_clean()
        prop.save()
    except ValidationError as exc:
        return _validation_error_payload(exc)

    return {
        "updated": True,
        "property": {
            "id": str(prop.pk),
            "name": prop.name,
            "status": prop.status,
            "address": prop.address,
            "city": prop.city,
            "property_category": prop.property_category,
            "primary_type": (
                prop.get_room_type_display()
                if prop.property_category == Property.PropertyCategory.ROOM
                else prop.get_unit_type_display()
            ),
            "bedrooms": prop.bedrooms,
            "bathrooms": str(prop.bathrooms) if prop.bathrooms is not None else None,
        },
        "applied": list(changes.keys()),
        "previous_name": previous_name,
        "message": (
            f"Renamed {previous_name} to {prop.name}."
            if "name" in changes
            else f"Updated {prop.name}: {', '.join(changes)}."
        ),
    }


def delete_property(
    landlord, *, property_query: str, pick: str = "", confirm: str = "",
) -> dict:
    """Delete listing. Blocked if any lease still references it (Lease.property PROTECT).
    On name collisions pass property_query=<id> or pick=first|no_group|with_group|2."""
    prop, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return _prop_err(err)

    blockers = property_delete_blockers(prop)
    if blockers:
        b = blockers[0]
        return {
            "error": f"Cannot delete {prop.name}: {b['detail']}",
            "leases": b.get("leases", []),
            "blockers": blockers,
        }

    preview = {
        "property": prop.name,
        "id": str(prop.pk),
        "inventory_items": prop.inventory_items.count(),
        "warning": "Deletes the listing and cascades private inventory/images.",
    }
    if not _confirmed(confirm):
        return _preview("delete_property", preview, "Permanently deletes the listing.")

    name = prop.name
    try:
        prop.delete()
    except ProtectedError as exc:
        return {"error": f"Delete blocked by related records: {exc}"}
    return {"deleted": True, "property": name}


def create_property_group(
    landlord, *, name: str, description: str = "", confirm: str = "",
) -> dict:
    from rentium.properties.models import PropertyGroup

    name = (name or "").strip()
    if not name:
        return {"error": "name is required."}
    # Idempotent by name. 12 of 20 logged failures on this tool were "You
    # already have a group named X" where the model had created that very group
    # a step earlier and was re-stating it as part of a longer sequence. A
    # create whose end state already holds has SUCCEEDED; erroring turns a
    # harmless restatement into a dead step and pushes the model into
    # improvising a recovery.
    existing = PropertyGroup.objects.filter(
        landlord=landlord, name__iexact=name,
    ).first()
    if existing is not None:
        return {
            "created": False,
            "reused": True,
            "group": {"id": str(existing.pk), "name": existing.name},
            "note": f"{existing.name!r} already exists — reusing it, nothing changed.",
        }

    preview = {"name": name, "description": (description or "")[:300]}
    if not _confirmed(confirm):
        return _preview(
            "create_property_group",
            preview,
            "Creates a property group for shared rooms (e.g. McKenzie Side Unit).",
        )

    g = PropertyGroup.objects.create(
        landlord=landlord, name=name[:100], description=(description or "")[:2000],
    )
    return {"created": True, "group": {"id": str(g.pk), "name": g.name}}


def assign_property_to_group(
    landlord,
    *,
    property_query: str,
    group_name: str = "",
    clear: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Attach a ROOM listing to a group, or clear group membership (clear=yes)."""
    from rentium.properties.models import Property
    from rentium.properties.services import assign_room_to_group

    prop, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return _prop_err(err)
    if prop.property_category != Property.PropertyCategory.ROOM:
        return {
            "error": "Only ROOM listings can belong to a property group "
            "(complete units must stay standalone).",
        }

    if _truthy(clear) or not group_name.strip():
        if _truthy(clear) and not prop.group_id:
            return {
                "unchanged": True,
                "property": prop.name,
                "group": None,
                "message": f"No change needed — {prop.name} is not in a group.",
            }
        if not _truthy(clear) and not group_name.strip():
            return {"error": "Pass group_name or clear=yes."}
        preview = {
            "property": prop.name,
            "from_group": prop.group.name if prop.group_id else None,
            "to_group": None,
            "action": "clear_group",
        }
        if not _confirmed(confirm):
            return _preview(
                "assign_property_to_group",
                preview,
                "Removes listing from its property group.",
            )
        try:
            prop = assign_room_to_group(prop, None)
        except ValidationError as exc:
            return _validation_error_payload(exc)
        return {
            "updated": True,
            "property": prop.name,
            "group": None,
            "message": f"Removed {prop.name} from {preview['from_group']}.",
        }

    group, gerr = _resolve_group(landlord, group_name)
    if gerr:
        return {"error": gerr}
    if prop.group_id == group.pk:
        return {
            "unchanged": True,
            "property": prop.name,
            "group": group.name,
            "message": f"No change needed — {prop.name} is already in {group.name}.",
        }

    preview = {
        "property": prop.name,
        "from_group": prop.group.name if prop.group_id else None,
        "to_group": group.name,
    }
    if not _confirmed(confirm):
        return _preview(
            "assign_property_to_group",
            preview,
            "Puts a room into a shared household group.",
        )

    try:
        prop = assign_room_to_group(prop, group)
    except ValidationError as exc:
        return _validation_error_payload(exc)
    return {
        "updated": True,
        "property": prop.name,
        "group": group.name,
        "message": f"Moved {prop.name} into {group.name}.",
    }


def create_group_room(
    landlord,
    *,
    name: str,
    group_name: str,
    inventory_items: str = "",
    shared_areas: str = "",
    shared_with_landlord: str = "",
    address: str = "",
    city: str = "",
    province: str = "",
    holding_name: str = "",
    confirm: str = "",
) -> dict:
    """Atomically create a room inside an existing group.

    Address/container data comes from the group's current rooms. An empty group
    can be bootstrapped from a named/exact-address holding plus any missing
    city/province. Private inventory, group membership, and group-wide common
    areas commit together.
    """
    from rentium.properties.models import InventoryItem
    from rentium.properties.models import Property
    from rentium.properties.models import PropertyArea
    from rentium.properties.services import create_group_common_area
    from rentium.properties.services import group_common_areas
    from rentium.properties.services import listing_name_conflicts
    from rentium.properties.services import parse_common_area_types

    room_name = (name or "").strip()
    if not room_name:
        return {"error": "name is required."}
    group, gerr = _resolve_group(landlord, group_name)
    if gerr:
        return {"error": gerr}
    derived, derr = _group_room_property_data(
        landlord,
        group,
        address=address,
        city=city,
        province=province,
        holding_name=holding_name,
    )
    if derr:
        return derr

    inventory = _parse_item_names(inventory_items)
    area_types = parse_common_area_types(shared_areas)
    if (shared_areas or "").strip() and not area_types:
        return {
            "error": (
                "No recognized shared area. Use names such as bathroom, kitchen, "
                "living room, laundry, hallway, or yard."
            ),
        }

    classification_raw = (shared_with_landlord or "").strip().casefold()
    truth_values = {"1", "true", "yes", "y", "on", "shared"}
    false_values = {"0", "false", "no", "n", "off", "tenant only", "tenants only"}
    classification_given = classification_raw in truth_values | false_values
    if classification_raw and not classification_given:
        return {
            "error": (
                "shared_with_landlord must be yes or no. It means whether the "
                "landlord or immediate relatives also use the common areas."
            ),
        }
    classification = classification_raw in truth_values

    existing_areas = {
        area.area_type: area for area in group_common_areas(group)
    }
    new_area_types = [area_type for area_type in area_types if area_type not in existing_areas]
    if new_area_types and not classification_given:
        labels = [
            str(PropertyArea.AreaType(area_type).label) for area_type in new_area_types
        ]
        return {
            "needs_input": True,
            "question_for_user": (
                "Does the landlord or an immediate relative also use the "
                f"{', '.join(labels)}? Please answer yes or no. This legal "
                "classification is required before I prepare the creation preview."
            ),
            "missing_field": "shared_with_landlord",
            "new_shared_areas": labels,
        }

    conflicts = listing_name_conflicts(landlord, room_name)
    exact = [row for row in conflicts if row["match"] == "exact"]
    near = [row for row in conflicts if row["match"] == "near"]

    area_preview = []
    for area_type in area_types:
        existing = existing_areas.get(area_type)
        desired_classification = (
            classification
            if classification_given
            else bool(existing.shared_with_landlord)
        )
        area_preview.append(
            {
                "type": area_type,
                "name": str(PropertyArea.AreaType(area_type).label),
                "action": "update_existing" if existing and classification_given else (
                    "keep_existing" if existing else "create"
                ),
                "shared_with_landlord": desired_classification,
            },
        )

    def existing_is_complete(prop) -> bool:
        if (
            prop.property_category != Property.PropertyCategory.ROOM
            or prop.group_id != group.pk
        ):
            return False
        existing_inventory = {
            value.casefold()
            for value in prop.inventory_items.values_list("name", flat=True)
        }
        if any(item.casefold() not in existing_inventory for item in inventory):
            return False
        current = {area.area_type: area for area in group_common_areas(group)}
        for row in area_preview:
            area = current.get(row["type"])
            if area is None or bool(area.shared_with_landlord) != bool(
                row["shared_with_landlord"],
            ):
                return False
        return True

    if exact:
        existing = Property.objects.filter(
            landlord=landlord,
            pk__in=[row["id"] for row in exact],
        ).first()
        if _confirmed(confirm) and existing is not None and existing_is_complete(existing):
            return {
                "created": False,
                "idempotent": True,
                "property": {
                    "id": str(existing.pk),
                    "name": existing.name,
                    "group": group.name,
                },
                "message": (
                    f"{existing.name} already exists in {group.name} with the "
                    "requested inventory and common areas; nothing was duplicated."
                ),
            }
        return {
            "error": (
                f"A listing named {room_name!r} already exists. "
                "Use that room or choose a distinct name."
            ),
            "name_conflicts": exact,
        }

    preview = {
        "name": room_name,
        "property_group": group.name,
        "derived_property_data": {
            "address": derived["address"],
            "city": derived["city"],
            "province": derived["province"],
            "postal_code": derived["postal_code"] or None,
            "country": derived["country"] or "Canada",
            "holding": derived["holding"].name,
        },
        "private_inventory_to_create": inventory,
        "shared_areas": area_preview,
        "name_conflicts": near,
        "duplicate_warning": (
            "A similarly named listing already exists. Review it before confirming."
            if near
            else None
        ),
        "atomic": True,
    }
    if not _confirmed(confirm):
        return _preview(
            "create_group_room",
            preview,
            "Creates the room, inventory, group membership, and common-area "
            "associations in one transaction.",
        )

    try:
        with transaction.atomic():
            # Re-lock and re-derive at execution time in case group data changed
            # after the preview.
            locked_group = type(group).objects.select_for_update().get(pk=group.pk)
            locked_derived, locked_error = _group_room_property_data(
                landlord,
                locked_group,
                address=address,
                city=city,
                province=province,
                holding_name=holding_name,
            )
            if locked_error:
                raise ValidationError(
                    locked_error.get("question_for_user")
                    or locked_error.get("error")
                    or "The group property data is no longer valid.",
                )
            if any(
                row["match"] == "exact"
                for row in listing_name_conflicts(landlord, room_name)
            ):
                raise ValidationError(
                    f"A listing matching {room_name!r} appeared after the preview.",
                )

            prop = Property(
                landlord=landlord,
                name=room_name[:255],
                address=locked_derived["address"][:255],
                city=locked_derived["city"][:100],
                province=locked_derived["province"],
                postal_code=locked_derived["postal_code"] or "",
                country=locked_derived["country"] or "Canada",
                address_verified=locked_derived["address_verified"],
                status=Property.PropertyStatus.AVAILABLE,
                property_category=Property.PropertyCategory.ROOM,
                room_type=Property.RoomType.PRIVATE,
                group=locked_group,
                holding=locked_derived["holding"],
            )
            prop.full_clean()
            prop.save()
            created_inventory = []
            for item_name in inventory:
                item = InventoryItem.objects.create(
                    property=prop,
                    name=item_name[:200],
                    quantity=1,
                    condition=InventoryItem.ItemCondition.GOOD,
                )
                created_inventory.append(item.name)

            created_areas = []
            for row in area_preview:
                area, was_created = create_group_common_area(
                    locked_group,
                    area_type=row["type"],
                    count=1,
                    shared_with_landlord=bool(row["shared_with_landlord"]),
                )
                created_areas.append(
                    {
                        "name": area.get_area_type_display(),
                        "created": was_created,
                        "shared_with_landlord": area.shared_with_landlord,
                    },
                )
    except ValidationError as exc:
        return _validation_error_payload(exc)
    except Exception as exc:  # noqa: BLE001 - transaction has already rolled back
        return {"error": f"Could not create the group room; nothing was saved: {exc}"}

    return {
        "created": True,
        "property": {
            "id": str(prop.pk),
            "name": prop.name,
            "group": locked_group.name,
            "holding": prop.holding.name,
            "address": prop.address,
        },
        "inventory": created_inventory,
        "shared_areas": created_areas,
        "message": (
            f"Created {prop.name} in {locked_group.name} with "
            f"{len(created_inventory)} private inventory item(s) and "
            f"{len(created_areas)} shared area association(s)."
        ),
    }


def create_holding(
    landlord, *, name: str, kind: str = "HOUSE", address: str = "", city: str = "",
    confirm: str = "",
) -> dict:
    """Create a holding: the physical/financial container for one address —
    one bank account, any mix of rooms and complete units (e.g. a garden
    suite + basement suite + upstairs rooms all under one house). kind:
    HOUSE|BUILDING|OTHER."""
    from rentium.properties.models import PropertyHolding

    name = (name or "").strip()
    if not name:
        return {"error": "name is required."}
    if PropertyHolding.objects.filter(landlord=landlord, name__iexact=name).exists():
        return {"error": f"You already have a holding named {name!r}."}
    kind_u = (kind or "HOUSE").strip().upper()
    if kind_u not in {c for c, _ in PropertyHolding.Kind.choices}:
        kind_u = PropertyHolding.Kind.HOUSE

    preview = {"name": name, "kind": kind_u, "address": address, "city": city}
    if not _confirmed(confirm):
        return _preview(
            "create_holding",
            preview,
            "Creates a holding (house/building) that listings can join.",
        )

    h = PropertyHolding.objects.create(
        landlord=landlord, name=name[:100], kind=kind_u,
        address=(address or "")[:255], city=(city or "")[:100],
    )
    return {"created": True, "holding": {"id": str(h.pk), "name": h.name, "kind": h.kind}}


def assign_property_to_holding(
    landlord, *, property_query: str, holding_name: str = "", clear: str = "",
    pick: str = "", confirm: str = "",
) -> dict:
    """Put a listing (ANY category — room or complete unit) into a holding,
    or clear=yes to remove it. Unlike property groups, holdings accept any
    listing type — this is what a bank-balance policy attaches to."""
    prop, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return _prop_err(err)

    if _truthy(clear) or not holding_name.strip():
        if not prop.holding_id and not holding_name.strip():
            return {"error": "Pass holding_name or clear=yes."}
        preview = {
            "property": prop.name,
            "from_holding": prop.holding.name if prop.holding_id else None,
            "to_holding": None,
            "action": "clear_holding",
        }
        if not _confirmed(confirm):
            return _preview(
                "assign_property_to_holding", preview, "Removes listing from its holding.",
            )
        prop.holding = None
        try:
            prop.full_clean()
            prop.save(update_fields=["holding", "updated_at"])
        except ValidationError as exc:
            return _validation_error_payload(exc)
        return {"updated": True, "property": prop.name, "holding": None}

    holding, herr = _resolve_holding(landlord, holding_name)
    if herr:
        return {"error": herr}

    preview = {
        "property": prop.name,
        "from_holding": prop.holding.name if prop.holding_id else None,
        "to_holding": holding.name,
    }
    if not _confirmed(confirm):
        return _preview(
            "assign_property_to_holding", preview, "Puts a listing into a holding.",
        )

    prop.holding = holding
    try:
        prop.full_clean()
        prop.save(update_fields=["holding", "updated_at"])
    except ValidationError as exc:
        return _validation_error_payload(exc)
    return {"updated": True, "property": prop.name, "holding": holding.name}


def list_holdings(landlord) -> dict:
    """List holdings (houses/buildings) and which listings belong to each."""
    from rentium.properties.models import PropertyHolding

    out = []
    for h in (
        PropertyHolding.objects.filter(landlord=landlord)
        .prefetch_related("listings")
        .order_by("name")
    ):
        out.append(
            {
                "id": str(h.pk),
                "name": h.name,
                "kind": h.kind,
                "address": h.address,
                "city": h.city,
                "listings": [p.name for p in h.listings.all()],
            },
        )
    return {"holdings": out, "count": len(out)}


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


def _resolve_security_deposit(
    *,
    security_deposit: str,
    total_rent: Decimal,
    asking_rent: Decimal | None = None,
) -> tuple[Decimal, str]:
    """
    Deposit defaults for landlord protection:
    - Explicit number (incl. 0) → use it.
    - Empty / omitted / 'default' / 'half' → half of monthly rent (capped
      common room practice; matches "$800 rent → $400 deposit").
    Pet deposit and cleaning fee stay 0 unless the landlord mentions them.
    """
    raw = str(security_deposit if security_deposit is not None else "").strip().lower()
    if raw in ("", "default", "half", "half_month", "auto"):
        base = total_rent if total_rent > 0 else (asking_rent or Decimal("0"))
        if base > 0:
            half = (base / Decimal("2")).quantize(Decimal("0.01"))
            return half, f"defaulted_to_half_month_rent ({half} = half of {base})"
        return Decimal("0"), "no_rent_basis_deposit_zero"
    try:
        return _money(raw), "explicit"
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def create_lease(
    landlord,
    *,
    property_query: str,
    start_date: str,
    end_date: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    pet_deposit: str = "0",
    cleaning_fee: str = "0",
    is_month_to_month: str = "0",
    pets_allowed: str = "0",
    smoking_allowed: str = "0",
    special_terms: str = "",
    etransfer_email: str = "",
    bills_included: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Create DRAFT lease. Defaults (landlord protection):
    - smoking_allowed / pets_allowed = false unless truthy
    - pet_deposit / cleaning_fee = 0 unless set
    - security_deposit: if omitted → half of total_rent; pass "0" for zero
    - etransfer_email: landlord service email fallback if blank
    """
    from rentium.leases.models import Lease

    prop, err = _resolve_property(landlord, property_query, pick=pick)
    if err:
        return _prop_err(err)

    try:
        start = _parse_date(start_date, "start_date")
    except ValueError as exc:
        return {"error": str(exc)}

    mtm = _truthy(is_month_to_month)
    end = None
    if not mtm:
        if not (end_date or "").strip():
            return {"error": "Fixed-term leases require end_date (YYYY-MM-DD)."}
        try:
            end = _parse_date(end_date, "end_date")
        except ValueError as exc:
            return {"error": str(exc)}
        if end <= start:
            return {"error": "end_date must be after start_date."}
    elif (end_date or "").strip():
        return {"error": "Month-to-month leases must not have end_date."}

    try:
        rent = _money(total_rent or "0")
        if rent <= 0 and prop.asking_rent:
            rent = Decimal(prop.asking_rent)
        deposit, deposit_src = _resolve_security_deposit(
            security_deposit=security_deposit,
            total_rent=rent,
            asking_rent=Decimal(prop.asking_rent or 0) if prop.asking_rent else None,
        )
        pet_dep = _money(pet_deposit or "0")
        clean_fee = _money(cleaning_fee or "0")
    except ValueError as exc:
        return {"error": str(exc)}

    # Essential-field gate: RENT. If the landlord never gave a rent and the
    # listing has no asking_rent to fall back on, do NOT silently create a $0
    # lease — stop and ask. An explicit total_rent="0" is respected (a genuinely
    # free room), mirroring the deposit "pass 0 for none" convention.
    if rent <= 0 and not (total_rent or "").strip():
        return _ask_for(
            f"What's the monthly rent for {prop.name}?",
            "When they answer, call create_lease again with the same details "
            "plus total_rent=<the amount>.",
        )

    # Defaults: no smoking / no pets unless landlord opts in (protection)
    pets = _truthy(pets_allowed)
    smoking = _truthy(smoking_allowed)

    e_email = (etransfer_email or "").strip()[:254]
    if not e_email:
        # Match UI fallback: service/account email
        e_email = (
            getattr(landlord, "service_email", None)
            or getattr(getattr(landlord, "user", None), "email", "")
            or ""
        )[:254]

    bills = {}
    raw_bills = (bills_included or "").strip()
    if raw_bills:
        if raw_bills.startswith("{"):
            try:
                import json

                bills = json.loads(raw_bills)
            except Exception:  # noqa: BLE001
                return {"error": "bills_included must be JSON object if provided."}
        else:
            # comma list → included utilities
            for part in raw_bills.replace(";", ",").split(","):
                key = part.strip().lower().replace(" ", "_")
                if key:
                    bills[key] = {"included": True}

    lease_type = _default_lease_type(prop)
    preview = {
        "property_id": str(prop.pk),
        "property": prop.name,
        "property_category": prop.property_category,
        "lease_type": lease_type,
        "lease_type_display": dict(Lease.LeaseType.choices).get(lease_type, lease_type),
        "status": Lease.LeaseStatus.DRAFT,
        "start_date": str(start),
        "end_date": str(end) if end else None,
        "is_month_to_month": mtm,
        "total_rent": str(rent),
        "security_deposit": str(deposit),
        "security_deposit_source": deposit_src,
        "pet_deposit": str(pet_dep),
        "cleaning_fee": str(clean_fee),
        "pets_allowed": pets,
        "smoking_allowed": smoking,
        "etransfer_email": e_email or None,
        "bills_included": bills or None,
        "special_terms": (special_terms or "")[:200] or None,
        "inventory_on_property": prop.inventory_items.count(),
        "warnings": (
            []
            if prop.inventory_items.exists()
            else [
                "Property has no inventory items — roommate agreement / "
                "condition inspection will show empty furnishings. "
                "Add via create_inventory_item or pass inventory_items on create_property.",
            ]
        ),
        "note": (
            "Created as DRAFT. Invite tenants separately. "
            "After invite: create_condition_inspection for move-in day "
            "(NOT schedule_viewing — that is only for showings)."
        ),
    }
    if not _confirmed(confirm):
        return _preview(
            "create_lease",
            preview,
            "Creates a DRAFT lease (UI: New Lease). Type auto-picked from listing. "
            "Omit security_deposit to default to half monthly rent; pass 0 for none.",
        )

    from rentium.leases.services import create_lease_record
    try:
        lease = create_lease_record(
            landlord=landlord,
            values={
                "property": prop,
                "group": None,
                "lease_type": lease_type,
                "status": Lease.LeaseStatus.DRAFT,
                "start_date": start,
                "end_date": end,
                "is_month_to_month": mtm,
                "move_in_date": start,
                "total_rent": rent,
                "security_deposit": deposit,
                "pet_deposit": pet_dep,
                "cleaning_fee": clean_fee,
                "pets_allowed": pets,
                "smoking_allowed": smoking,
                "special_terms": (special_terms or "")[:5000],
                "etransfer_email": e_email,
                "bills_included": bills or {},
            },
        )
    except ValidationError as exc:
        return _validation_error_payload(exc)

    return {
        "created": True,
        "lease": {
            "id": str(lease.pk),
            "lease_number": lease.lease_number,
            "status": lease.status,
            "lease_type": lease.lease_type,
            "lease_type_display": lease.get_lease_type_display(),
            "property": prop.name,
            "start_date": str(lease.start_date),
            "end_date": str(lease.end_date) if lease.end_date else None,
            "total_rent": str(lease.total_rent),
            "security_deposit": str(lease.security_deposit),
            "security_deposit_source": deposit_src,
            "pet_deposit": str(lease.pet_deposit),
            "cleaning_fee": str(lease.cleaning_fee),
            "pets_allowed": lease.pets_allowed,
            "smoking_allowed": lease.smoking_allowed,
            "etransfer_email": lease.etransfer_email or None,
        },
        "next_steps": [
            "Invite tenants with add_roommate_to_lease or invite_tenant_to_lease",
            "Landlord may landlord_sign_lease once rent is fully allocated",
            "create_condition_inspection after at least one tenant is on the lease",
            "Lease PDF is always downloadable via the UI /api/leases/<id>/pdf/ "
            "(does not require a stored document_file)",
        ],
    }


def update_lease(
    landlord,
    *,
    property_query: str = "",
    lease_number: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    start_date: str = "",
    end_date: str = "",
    pets_allowed: str = "",
    smoking_allowed: str = "",
    special_terms: str = "",
    house_rules: str = "",
    shared_with: str = "",
    bills: str = "",
    etransfer_email: str = "",
    is_month_to_month: str = "",
    confirm: str = "",
) -> dict:

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)

    # Match LeaseNotLocked: ACTIVE+ cannot be edited via API
    if lease.is_locked():
        return {
            "error": (
                f"Lease {lease.lease_number} is locked (status={lease.status}). "
                "ACTIVE/EXPIRED/TERMINATED/RENEWED leases cannot be field-edited "
                "(same as UI LeaseNotLocked). Use terminate_lease or admin."
            ),
        }

    changes: dict = {}
    try:
        if total_rent != "":
            changes["total_rent"] = _money(total_rent)
        if security_deposit != "":
            changes["security_deposit"] = _money(security_deposit)
        if start_date.strip():
            changes["start_date"] = _parse_date(start_date, "start_date")
        if is_month_to_month != "":
            mtm = _truthy(is_month_to_month)
            changes["is_month_to_month"] = mtm
            if mtm:
                changes["end_date"] = None
        if end_date != "" and not changes.get("is_month_to_month"):
            if end_date.strip() == "" and _truthy(is_month_to_month):
                changes["end_date"] = None
            elif end_date.strip():
                changes["end_date"] = _parse_date(end_date, "end_date")
        if pets_allowed != "":
            changes["pets_allowed"] = _truthy(pets_allowed)
        if smoking_allowed != "":
            changes["smoking_allowed"] = _truthy(smoking_allowed)
        if special_terms != "":
            changes["special_terms"] = special_terms[:5000]
        if house_rules != "":
            # Roommate-agreement house rules (extra clauses the landlord adds).
            changes["house_rules"] = house_rules[:5000]
        if shared_with != "":
            # Who else uses the shared common areas (roommate agreements): a
            # subset of landlord / roommates / landlord_relatives.
            _map = {
                "landlord": "LANDLORD", "the landlord": "LANDLORD",
                "roommate": "ROOMMATES", "roommates": "ROOMMATES",
                "other roommates": "ROOMMATES",
                "relatives": "LANDLORD_RELATIVES",
                "landlord relatives": "LANDLORD_RELATIVES",
                "landlord_relatives": "LANDLORD_RELATIVES",
                "landlord's relatives": "LANDLORD_RELATIVES",
            }
            vals: list[str] = []
            for tok in shared_with.replace(";", ",").split(","):
                v = _map.get(tok.strip().lower())
                if v and v not in vals:
                    vals.append(v)
            changes["common_space_shared_with"] = vals
        if bills != "":
            # "water included, hydro tenant pays, internet included" → merge onto
            # the existing bills_included (same shape the UI editor writes).
            util_map = {
                "electricity": "electricity", "hydro": "electricity",
                "power": "electricity", "electric": "electricity",
                "water": "water", "gas": "gas", "heat": "heat", "heating": "heat",
                "internet": "internet", "wifi": "internet", "wi-fi": "internet",
                "cable": "cable", "tv": "cable",
                "garbage": "waste", "trash": "waste", "waste": "waste",
                "recycling": "waste", "sewer": "sewer", "sewage": "sewer",
            }
            merged = dict(lease.bills_included or {})
            for part in bills.replace(";", ",").split(","):
                s = part.strip().lower()
                if not s:
                    continue
                util = next((v for k, v in util_map.items() if k in s), None)
                if not util:
                    continue
                included = "includ" in s and "not includ" not in s
                entry = {"provider": "", "category": util, "notes": ""}
                if included:
                    entry["included"] = True
                    entry["tenant_responsibility"] = {}
                else:  # tenant pays / not included
                    entry["included"] = False
                    entry["tenant_responsibility"] = {
                        "type": "percentage", "value": 100, "distribution": "equal",
                    }
                merged[util] = entry
            if merged != (lease.bills_included or {}):
                changes["bills_included"] = merged
        if etransfer_email != "":
            changes["etransfer_email"] = etransfer_email.strip()[:254]
    except ValueError as exc:
        return {"error": str(exc)}

    if not changes:
        return {"error": "No lease fields to update."}

    preview = {
        "lease_number": lease.lease_number,
        "property": lease.property.name if lease.property_id else "",
        "status": lease.status,
        "changes": {
            k: (str(v) if isinstance(v, (Decimal, date)) else v)
            for k, v in changes.items()
        },
    }
    if not _confirmed(confirm):
        return _preview(
            "update_lease",
            preview,
            "Updates draft/pending lease fields only.",
        )

    for k, v in changes.items():
        setattr(lease, k, v)
    try:
        lease.full_clean()
        lease.save()
    except ValidationError as exc:
        return _validation_error_payload(exc)

    # If total_rent changed, rebalance unsigned tenant shares (UI equal-split)
    if "total_rent" in changes:
        from rentium.rama.domain_actions import rebalance_lease_rent_shares

        rebalance_lease_rent_shares(lease, force_equal_unsigned=True)

    return {
        "updated": True,
        "lease_number": lease.lease_number,
        "status": lease.status,
        "total_rent": str(lease.total_rent),
        "applied": list(changes.keys()),
    }


def adjust_lease(
    landlord,
    *,
    lease_number: str = "",
    property_query: str = "",
    start_date: str = "",
    end_date: str = "",
    furnishing: str = "",
    inventory_items: str = "",
    special_terms: str = "",
    confirm: str = "",
) -> dict:
    """Edit a DRAFT or PENDING (not yet ACTIVE) lease in one confirmed step.

    Use this when the landlord wants to change start/end dates and/or
    furnished status. Furnishing is NOT a lease checkbox — it is derived from
    the listing's private inventory (a bed makes a room furnished). This tool
    updates dates on the lease and, when asked, ensures inventory so the lease
    PDF shows furnished correctly.

    furnishing: furnished | semi_furnished | unfurnished (optional).
    inventory_items: optional explicit list (e.g. 'Queen bed, Mattress, Desk').
    """
    from rentium.properties.models import InventoryItem

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)
    if lease.is_locked():
        return {
            "error": (
                f"Lease {lease.lease_number} is {lease.status} and locked. "
                "ACTIVE/EXPIRED/TERMINATED/RENEWED leases cannot have start "
                "date or terms rewritten. Create a new lease instead."
            ),
        }
    if not lease.property_id:
        return {"error": "This lease has no listing attached."}

    prop = lease.property
    changes: dict = {}
    inventory_to_add: list[str] = []
    furnishing_note = ""

    try:
        if start_date.strip():
            changes["start_date"] = _parse_date(start_date, "start_date")
        if end_date.strip():
            changes["end_date"] = _parse_date(end_date, "end_date")
        if special_terms != "":
            changes["special_terms"] = special_terms[:5000]
    except ValueError as exc:
        return {"error": str(exc)}

    if (
        changes.get("start_date")
        and changes.get("end_date")
        and changes["end_date"] < changes["start_date"]
    ):
        return {"error": "end_date must be on or after start_date."}
    if (
        changes.get("start_date")
        and not changes.get("end_date")
        and lease.end_date
        and lease.end_date < changes["start_date"]
    ):
        return {
            "error": (
                f"New start date {changes['start_date']} is after the current "
                f"end date {lease.end_date}. Pass end_date too."
            ),
        }

    mode = (
        (furnishing or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if mode in ("furnished", "fully_furnished", "full"):
        mode = "furnished"
    elif mode in ("semi", "semi_furnished", "semifurnished", "partially_furnished"):
        mode = "semi_furnished"
    elif mode in ("unfurnished", "un_furnished", "none", "empty"):
        mode = "unfurnished"
    elif mode:
        return {
            "error": (
                "furnishing must be furnished, semi_furnished, or unfurnished "
                f"(got {furnishing!r})."
            ),
        }

    explicit_items = []
    raw_items = (inventory_items or "").strip()
    if raw_items:
        if raw_items.startswith("["):
            import json

            try:
                explicit_items = [
                    str(x).strip() for x in json.loads(raw_items) if str(x).strip()
                ]
            except Exception:  # noqa: BLE001
                return {"error": "inventory_items JSON list is invalid."}
        else:
            explicit_items = [
                p.strip() for p in raw_items.replace(";", ",").split(",") if p.strip()
            ]

    existing_names = {
        n.casefold()
        for n in prop.inventory_items.values_list("name", flat=True)
    }

    def _need(name: str) -> None:
        if name.casefold() not in existing_names and name not in inventory_to_add:
            inventory_to_add.append(name)

    if explicit_items:
        for name in explicit_items:
            _need(name)
    elif mode == "furnished":
        _need("Queen bed")
        _need("Mattress")
        _need("Dresser")
        _need("Nightstand")
        furnishing_note = "Room is furnished."
    elif mode == "semi_furnished":
        _need("Queen bed")
        _need("Mattress")
        furnishing_note = "Room is semi-furnished (bed included; other items limited)."
    elif mode == "unfurnished":
        furnishing_note = (
            "Landlord asked for unfurnished. Existing inventory is left as-is "
            "(removing beds would change the listing for all future leases — "
            "say remove inventory items if you want that)."
        )

    # Surface furnishing note in special_terms when not already present.
    if furnishing_note and mode in ("furnished", "semi_furnished"):
        current_terms = changes.get("special_terms", lease.special_terms or "")
        if "semi-furnished" not in current_terms.casefold() and mode == "semi_furnished":
            changes["special_terms"] = (
                (current_terms + "\n\n" if current_terms else "")
                + furnishing_note
            ).strip()[:5000]
        elif "furnished" not in current_terms.casefold() and mode == "furnished":
            changes["special_terms"] = (
                (current_terms + "\n\n" if current_terms else "")
                + furnishing_note
            ).strip()[:5000]

    if not changes and not inventory_to_add and not (mode == "unfurnished"):
        return {
            "error": (
                "Nothing to change. Pass start_date and/or end_date and/or "
                "furnishing=furnished|semi_furnished|unfurnished."
            ),
        }

    will_be_furnished = bool(prop.is_furnished) or bool(inventory_to_add)
    preview = {
        "lease_number": lease.lease_number,
        "property": prop.name,
        "status": lease.status,
        "editable": True,
        "note": (
            "PENDING/DRAFT leases can be field-edited. Furnishing is derived "
            "from inventory on the listing (a bed → furnished on the PDF)."
        ),
        "lease_changes": {
            k: (str(v) if not isinstance(v, (list, dict)) else v)
            for k, v in changes.items()
        },
        "inventory_to_add": inventory_to_add or None,
        "furnishing": mode or None,
        "property_is_furnished_now": bool(prop.is_furnished),
        "property_will_read_as_furnished": will_be_furnished if mode else bool(prop.is_furnished),
        "unfurnished_note": furnishing_note if mode == "unfurnished" else None,
    }
    if not _confirmed(confirm):
        return _preview(
            "adjust_lease",
            preview,
            "Updates the unlocked lease and listing inventory in one step.",
        )

    applied: list[str] = []
    if changes:
        for key, value in changes.items():
            setattr(lease, key, value)
        try:
            lease.full_clean()
            lease.save()
        except ValidationError as exc:
            return _validation_error_payload(exc)
        applied.extend(changes.keys())

    created_items = []
    for name in inventory_to_add:
        item = InventoryItem.objects.create(
            property=prop,
            name=name[:200],
            quantity=1,
            condition=InventoryItem.ItemCondition.GOOD,
        )
        created_items.append({"id": str(item.pk), "name": item.name})
    if created_items:
        applied.append("inventory")
        prop.refresh_from_db()

    return {
        "updated": True,
        "lease_number": lease.lease_number,
        "status": lease.status,
        "start_date": str(lease.start_date),
        "end_date": str(lease.end_date) if lease.end_date else None,
        "property": prop.name,
        "property_is_furnished": bool(prop.is_furnished),
        "inventory_added": created_items,
        "applied": applied,
        "message": (
            f"Updated {lease.lease_number}: "
            + ", ".join(applied)
            + (
                f". Listing now reads "
                f"{'furnished' if prop.is_furnished else 'unfurnished'}."
                if "inventory" in applied or mode
                else "."
            )
        ),
    }


def delete_draft_lease(
    landlord, *, property_query: str = "", lease_number: str = "", confirm: str = "",
) -> dict:
    from rentium.leases.models import Lease

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)

    if lease.status != Lease.LeaseStatus.DRAFT:
        blocker = delete_draft_lease_blockers(
            landlord, lease_number=lease.lease_number,
        )[0]
        return {"error": blocker["detail"]}

    preview = {
        "lease_number": lease.lease_number,
        "property": lease.property.name if lease.property_id else "",
        "status": lease.status,
        "tenants": lease.lease_tenants.count(),
    }
    if not _confirmed(confirm):
        return _preview("delete_draft_lease", preview, "Permanently deletes a DRAFT lease.")

    ln = lease.lease_number
    try:
        lease.delete()
    except ProtectedError:
        return {
            "error": (
                "This draft can't be deleted because it already has payment "
                "records attached (same as UI)."
            ),
        }
    return {"deleted": True, "lease_number": ln}


def terminate_lease(
    landlord,
    *,
    property_query: str = "",
    lease_number: str = "",
    termination_date: str = "",
    move_out_date: str = "",
    confirm: str = "",
) -> dict:
    """Mirror LeaseViewSet.terminate — status TERMINATED, void open charges."""
    from rentium.leases.models import Lease
    from rentium.leases.occupancy import close_lease_occupancies
    from rentium.ledger.billing import void_open_charges_for_lease

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)

    blockers = lease_terminate_blockers(lease)
    if blockers:
        return {"error": blockers[0]["detail"]}

    try:
        term = (
            _parse_date(termination_date, "termination_date")
            if (termination_date or "").strip()
            else timezone.now().date()
        )
        mout = (
            _parse_date(move_out_date, "move_out_date")
            if (move_out_date or "").strip()
            else term
        )
    except ValueError as exc:
        return {"error": str(exc)}

    preview = {
        "lease_number": lease.lease_number,
        "property": lease.property.name if lease.property_id else "",
        "from_status": lease.status,
        "to_status": Lease.LeaseStatus.TERMINATED,
        "termination_date": str(term),
        "move_out_date": str(mout),
        "side_effects": [
            "void open (unpaid) charges",
            "close occupancy records",
            "preserve lease row for audit",
        ],
    }
    if not _confirmed(confirm):
        return _preview(
            "terminate_lease",
            preview,
            "Terminates lease like the UI Terminate button.",
        )

    with transaction.atomic():
        lease.status = Lease.LeaseStatus.TERMINATED
        # move_out may be "today" even if term starts in the future — clamp
        # so model.clean() (end >= start, move_out >= move_in) still holds.
        if lease.start_date and mout < lease.start_date:
            mout = lease.start_date
        lease.move_out_date = mout
        if not lease.end_date or lease.end_date > term:
            end = term
            if lease.start_date and end < lease.start_date:
                end = lease.start_date
            lease.end_date = end
        try:
            lease.full_clean()
        except ValidationError as exc:
            return _validation_error_payload(exc)
        lease.save()
        void_open_charges_for_lease(
            lease,
            reason=f"Lease {lease.lease_number} terminated {term}",
            created_by=landlord.user,
        )
        close_lease_occupancies(lease, move_out=mout)

    return {
        "terminated": True,
        "lease_number": lease.lease_number,
        "status": str(lease.status),
        "end_date": str(lease.end_date) if lease.end_date else None,
        "move_out_date": str(lease.move_out_date) if lease.move_out_date else None,
    }


def landlord_sign_lease(
    landlord,
    *,
    property_query: str = "",
    lease_number: str = "",
    confirm: str = "",
) -> dict:
    """Mirror LeaseViewSet.landlord_sign — requires full rent allocation."""
    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)

    if lease.is_locked():
        return {"error": "This lease is already fully executed (locked)."}
    if lease.landlord_signed:
        return {"error": "You have already signed this lease."}
    if not lease.rent_is_fully_allocated():
        return {
            "error": (
                f"Rent isn't fully assigned — "
                f"${lease.get_unallocated_rent()} of ${lease.total_rent} still "
                f"unassigned. Adjust tenant shares (rebalance_lease_rents) first."
            ),
        }

    preview = {
        "lease_number": lease.lease_number,
        "property": lease.property.name if lease.property_id else "",
        "status": lease.status,
        "total_rent": str(lease.total_rent),
        "unallocated_rent": str(lease.get_unallocated_rent()),
        "note": "With any tenant signature, lease may activate (charges generated).",
    }
    if not _confirmed(confirm):
        return _preview(
            "landlord_sign_lease",
            preview,
            "Records landlord signature (may activate lease).",
        )

    lease.landlord_signed = True
    lease.landlord_signed_date = timezone.now()
    lease.save(
        update_fields=["landlord_signed", "landlord_signed_date", "updated_at"],
    )
    activated = lease.check_and_activate()
    return {
        "signed": True,
        "lease_number": lease.lease_number,
        "status": lease.status,
        "landlord_signed": True,
        "activated": bool(activated),
    }


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def _resolve_work_order(landlord, *, work_order_id: str = "", title_query: str = ""):
    from rentium.maintenance.models import WorkOrder

    if work_order_id:
        try:
            wo = (
                WorkOrder.objects.for_landlord(landlord)
                .select_related("property", "unit")
                .get(pk=work_order_id)
            )
            return wo, None
        except (WorkOrder.DoesNotExist, ValueError):
            return None, f"No work order {work_order_id!r}."
    if title_query:
        qs = WorkOrder.objects.for_landlord(landlord).filter(
            title__icontains=title_query.strip(),
        )
        if qs.count() != 1:
            return None, (
                f"Need exactly one WO matching {title_query!r} "
                f"(found {qs.count()}). Pass work_order_id."
            )
        return qs.select_related("property").first(), None
    return None, "Pass work_order_id or title_query."


def update_work_order(
    landlord,
    *,
    work_order_id: str = "",
    title_query: str = "",
    title: str = "",
    description: str = "",
    priority: str = "",
    category: str = "",
    contractor_name: str = "",
    contractor_phone: str = "",
    scheduled_date: str = "",
    confirm: str = "",
) -> dict:
    """Update fields only — status goes through transition_work_order / complete."""
    from rentium.maintenance.models import WorkOrder

    wo, err = _resolve_work_order(
        landlord, work_order_id=work_order_id, title_query=title_query,
    )
    if err:
        return _prop_err(err)

    if wo.status in (WorkOrder.Status.COMPLETED, WorkOrder.Status.CANCELLED):
        return {
            "error": f"Cannot edit a {wo.status} work order (terminal status).",
        }

    changes: dict = {}
    if title.strip():
        changes["title"] = title.strip()[:200]
    if description != "":
        changes["description"] = description[:5000]
    if priority.strip():
        pr = priority.strip().upper()
        if pr not in WorkOrder.Priority.values:
            return {"error": f"Invalid priority. Use {list(WorkOrder.Priority.values)}"}
        changes["priority"] = pr
    if category.strip():
        cat = category.strip().upper()
        if cat not in WorkOrder.Category.values:
            return {"error": f"Invalid category. Use {list(WorkOrder.Category.values)}"}
        changes["category"] = cat
    if contractor_name != "":
        changes["contractor_name"] = contractor_name.strip()[:150]
    if contractor_phone != "":
        changes["contractor_phone"] = contractor_phone.strip()[:30]
    if scheduled_date.strip():
        try:
            changes["scheduled_date"] = _parse_date(scheduled_date, "scheduled_date")
        except ValueError as exc:
            return {"error": str(exc)}

    if not changes:
        return {
            "error": "No fields to update. For status use transition_work_order.",
        }

    preview = {
        "id": str(wo.pk),
        "title": wo.title,
        "property": wo.property.name,
        "status": wo.status,
        "changes": {
            k: (str(v) if isinstance(v, date) else v) for k, v in changes.items()
        },
        "note": "Status is NOT changed here — use transition_work_order / complete_work_order.",
    }
    if not _confirmed(confirm):
        return _preview("update_work_order", preview, "Updates WO metadata fields.")

    for k, v in changes.items():
        setattr(wo, k, v)
    try:
        wo.full_clean()
        wo.save()
    except ValidationError as exc:
        return _validation_error_payload(exc)

    return {
        "updated": True,
        "work_order": {
            "id": str(wo.pk),
            "title": wo.title,
            "status": wo.status,
            "priority": wo.priority,
            "contractor_name": wo.contractor_name,
        },
        "applied": list(changes.keys()),
    }


def complete_work_order(
    landlord,
    *,
    work_order_id: str = "",
    title_query: str = "",
    cost: str = "",
    post_expense: str = "0",
    vendor: str = "",
    confirm: str = "",
) -> dict:
    """Mirror WorkOrderViewSet.complete — FSM to COMPLETED + optional expense."""
    from rentium.maintenance.models import WorkOrder

    wo, err = _resolve_work_order(
        landlord, work_order_id=work_order_id, title_query=title_query,
    )
    if err:
        return _prop_err(err)

    cost_dec = None
    if str(cost).strip():
        try:
            cost_dec = _money(cost)
        except ValueError as exc:
            return {"error": str(exc)}

    will_expense = bool(cost_dec is not None and _truthy(post_expense))
    preview = {
        "id": str(wo.pk),
        "title": wo.title,
        # place_name covers both targets — a shared-space job has no listing,
        # and reading .property.name here crashed the whole completion.
        "property": wo.place_name,
        "from_status": wo.status,
        "to_status": WorkOrder.Status.COMPLETED,
        "cost": str(cost_dec) if cost_dec is not None else None,
        "post_expense": will_expense,
        "charges_tenant": (
            wo.responsible_tenant.user.name
            if wo.tenant_chargeable and wo.responsible_tenant_id
            else None
        ),
        "vendor": (vendor or wo.contractor_name or "")[:150],
    }
    if not _confirmed(confirm):
        return _preview(
            "complete_work_order",
            preview,
            "Completes the job; optionally posts a MAINTENANCE expense.",
        )

    if cost_dec is not None:
        wo.cost = cost_dec
        wo.save(update_fields=["cost", "updated_at"])

    try:
        wo.transition_to(WorkOrder.Status.COMPLETED, by=landlord.user)
    except ValidationError as exc:
        return _validation_error_payload(exc)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Transition failed: {exc}"}

    # A tenant-caused repair also raises a CLAIM against that tenant. It is a
    # debt they owe, NOT a deposit deduction — BC RTA lets a landlord keep
    # deposit money only with written agreement or an RTB application within 15
    # days, and getting that wrong costs double the deposit.
    claim_id = None
    if cost_dec is not None:
        wo.cost = cost_dec
        claim_id = _raise_tenant_claim(landlord, wo)

    expense_id = None
    if will_expense and cost_dec is not None:
        from rentium.ledger.services import post_expense

        # A shared-space repair has no listing to charge, but it DOES have an
        # address. Passing the unit's holding keeps the expense attributable
        # instead of landing nowhere — and stops it being charged to whichever
        # single room happened to be mentioned.
        entry, _created = post_expense(
            landlord=landlord,
            property=wo.property,
            holding=(
                wo.property.holding
                if wo.property_id
                else (wo.unit.holding if wo.unit_id else None)
            ),
            amount=cost_dec,
            category="MAINTENANCE",
            description=f"Work order: {wo.title} ({wo.place_name})",
            vendor=vendor or wo.contractor_name,
            work_order=wo,
            idempotency_key=f"woexp:{wo.pk}",
            created_by=landlord.user,
        )
        expense_id = str(getattr(entry, "pk", "") or "")

    return {
        "completed": True,
        "work_order": {
            "id": str(wo.pk),
            "title": wo.title,
            "status": wo.status,
            "cost": str(wo.cost) if wo.cost is not None else None,
        },
        "expense_posted": will_expense,
        "expense_id": expense_id or None,
        "tenant_claim_id": claim_id,
        "tenant_claim_note": (
            (
                f"Claimed {cost_dec} from {wo.responsible_tenant.user.name}. This "
                "is a debt they owe — it does NOT come out of their deposit "
                "unless they agree in writing or you apply to the RTB within 15 "
                "days of the tenancy ending."
            )
            if claim_id
            else None
        ),
    }


def add_work_order_comment(
    landlord,
    *,
    body: str,
    work_order_id: str = "",
    title_query: str = "",
    confirm: str = "",
) -> dict:
    from rentium.maintenance.models import WorkOrderComment

    wo, err = _resolve_work_order(
        landlord, work_order_id=work_order_id, title_query=title_query,
    )
    if err:
        return _prop_err(err)
    body = (body or "").strip()
    if not body:
        return {"error": "body is required."}

    preview = {
        "work_order": wo.title,
        "property": wo.property.name,
        "body": body[:500],
    }
    if not _confirmed(confirm):
        return _preview("add_work_order_comment", preview, "Adds a landlord comment.")

    c = WorkOrderComment.objects.create(
        work_order=wo, author=landlord.user, body=body[:5000],
    )
    return {
        "created": True,
        "comment_id": str(c.pk),
        "work_order_id": str(wo.pk),
    }


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def create_inventory_item(
    landlord,
    *,
    property_query: str,
    name: str,
    quantity: str = "1",
    condition: str = "GOOD",
    location: str = "",
    description: str = "",
    confirm: str = "",
) -> dict:
    from rentium.properties.models import InventoryItem

    prop, err = _resolve_property(landlord, property_query)
    if err:
        return _prop_err(err)
    name = (name or "").strip()
    if not name:
        return {"error": "name is required."}
    try:
        qty = max(1, int(str(quantity or "1").strip() or "1"))
    except ValueError:
        return {"error": f"Invalid quantity {quantity!r}."}
    cond = (condition or "GOOD").strip().upper()
    if cond and cond not in InventoryItem.ItemCondition.values:
        return {
            "error": f"Invalid condition. Use {list(InventoryItem.ItemCondition.values)}",
        }

    preview = {
        "property": prop.name,
        "name": name,
        "quantity": qty,
        "condition": cond or None,
        "location": (location or "")[:255],
        "scope": "private",
    }
    if not _confirmed(confirm):
        return _preview(
            "create_inventory_item",
            preview,
            "Adds private inventory; may flip is_furnished via signals.",
        )

    item = InventoryItem.objects.create(
        property=prop,
        name=name[:200],
        quantity=qty,
        condition=cond or None,
        location_description=(location or "")[:255],
        description=(description or "")[:2000],
    )
    prop.refresh_from_db()
    return {
        "created": True,
        "item": {
            "id": str(item.pk),
            "name": item.name,
            "quantity": item.quantity,
            "condition": item.condition,
            "property": prop.name,
        },
        "property_is_furnished": bool(prop.is_furnished),
    }


def update_inventory_item(
    landlord,
    *,
    property_query: str,
    item_name: str,
    name: str = "",
    quantity: str = "",
    condition: str = "",
    location: str = "",
    description: str = "",
    confirm: str = "",
) -> dict:
    from rentium.properties.models import InventoryItem

    prop, err = _resolve_property(landlord, property_query)
    if err:
        return _prop_err(err)
    q = (item_name or "").strip()
    if not q:
        return {"error": "item_name is required."}
    qs = prop.inventory_items.filter(name__icontains=q)
    if qs.count() != 1:
        return {
            "error": f"Need exactly one inventory item matching {item_name!r} "
            f"on {prop.name} (found {qs.count()}).",
            "candidates": list(qs.values_list("name", flat=True)[:8]),
        }
    item = qs.first()

    changes: dict = {}
    if name.strip():
        changes["name"] = name.strip()[:200]
    if quantity != "":
        try:
            changes["quantity"] = max(1, int(str(quantity).strip()))
        except ValueError:
            return {"error": f"Invalid quantity {quantity!r}."}
    if condition.strip():
        cond = condition.strip().upper()
        if cond not in InventoryItem.ItemCondition.values:
            return {"error": f"Invalid condition {condition!r}."}
        changes["condition"] = cond
    if location != "":
        changes["location_description"] = location[:255]
    if description != "":
        changes["description"] = description[:2000]
    if not changes:
        return {"error": "No inventory fields to update."}

    preview = {
        "property": prop.name,
        "item": item.name,
        "changes": changes,
    }
    if not _confirmed(confirm):
        return _preview("update_inventory_item", preview, "Updates private inventory.")

    # Captured BEFORE the write so the autonomy tier can offer an exact undo
    # (tool_meta._undo_update_inventory_item). Only the fields actually being
    # overwritten are recorded.
    previous = {k: getattr(item, k) for k in changes}
    for k, v in changes.items():
        setattr(item, k, v)
    item.save()
    return {
        "updated": True,
        "previous": previous,
        "item": {
            "id": str(item.pk),
            "name": item.name,
            "quantity": item.quantity,
            "condition": item.condition,
        },
    }


def delete_inventory_item(
    landlord, *, property_query: str, item_name: str, confirm: str = "",
) -> dict:
    prop, err = _resolve_property(landlord, property_query)
    if err:
        return _prop_err(err)
    q = (item_name or "").strip()
    qs = prop.inventory_items.filter(name__icontains=q)
    if qs.count() != 1:
        return {
            "error": f"Need exactly one item matching {item_name!r} "
            f"(found {qs.count()}).",
            "candidates": list(qs.values_list("name", flat=True)[:8]),
        }
    item = qs.first()
    preview = {"property": prop.name, "item": item.name, "id": str(item.pk)}
    if not _confirmed(confirm):
        return _preview("delete_inventory_item", preview, "Deletes private inventory item.")
    nm = item.name
    item.delete()
    prop.refresh_from_db()
    return {
        "deleted": True,
        "item": nm,
        "property": prop.name,
        "property_is_furnished": bool(prop.is_furnished),
    }


def create_shared_inventory_item(
    landlord,
    *,
    group_name: str,
    name: str,
    quantity: str = "1",
    condition: str = "GOOD",
    location: str = "",
    description: str = "",
    confirm: str = "",
) -> dict:
    from rentium.properties.models import SharedInventoryItem

    group, err = _resolve_group(landlord, group_name)
    if err:
        return _prop_err(err)
    name = (name or "").strip()
    if not name:
        return {"error": "name is required."}
    try:
        qty = max(1, int(str(quantity or "1").strip() or "1"))
    except ValueError:
        return {"error": f"Invalid quantity {quantity!r}."}
    cond = (condition or "GOOD").strip().upper()
    if cond and cond not in SharedInventoryItem.ItemCondition.values:
        return {
            "error": f"Invalid condition. Use {list(SharedInventoryItem.ItemCondition.values)}",
        }

    preview = {
        "group": group.name,
        "name": name,
        "quantity": qty,
        "condition": cond,
        "location": (location or "")[:255],
        "scope": "shared",
    }
    if not _confirmed(confirm):
        return _preview(
            "create_shared_inventory_item",
            preview,
            "Adds shared inventory on a property group (kitchen etc.).",
        )

    item = SharedInventoryItem.objects.create(
        group=group,
        name=name[:200],
        quantity=qty,
        condition=cond or None,
        location_description=(location or "")[:255],
        description=(description or "")[:2000],
    )
    return {
        "created": True,
        "item": {
            "id": str(item.pk),
            "name": item.name,
            "group": group.name,
            "quantity": item.quantity,
        },
    }


def delete_shared_inventory_item(
    landlord, *, group_name: str, item_name: str, confirm: str = "",
) -> dict:
    group, err = _resolve_group(landlord, group_name)
    if err:
        return _prop_err(err)
    q = (item_name or "").strip()
    qs = group.group_shared_inventory.filter(name__icontains=q)
    if qs.count() != 1:
        return {
            "error": f"Need exactly one shared item matching {item_name!r} "
            f"(found {qs.count()}).",
            "candidates": list(qs.values_list("name", flat=True)[:8]),
        }
    item = qs.first()
    preview = {"group": group.name, "item": item.name}
    if not _confirmed(confirm):
        return _preview(
            "delete_shared_inventory_item", preview, "Deletes shared inventory item.",
        )
    nm = item.name
    item.delete()
    return {"deleted": True, "item": nm, "group": group.name}


# ---------------------------------------------------------------------------
# Full room → lease → invite → inspection workflow (one confirm)
# ---------------------------------------------------------------------------


def setup_room_tenancy(
    landlord,
    *,
    room_name: str,
    address: str,
    city: str,
    group_name: str = "",
    province: str = "bc",
    asking_rent: str = "",
    inventory_items: str = "",
    start_date: str = "",
    end_date: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    pet_deposit: str = "0",
    cleaning_fee: str = "0",
    special_terms: str = "",
    tenant_name: str = "",
    tenant_email: str = "",
    smoking_allowed: str = "0",
    pets_allowed: str = "0",
    create_inspection: str = "1",
    use_existing_if_name_matches: str = "1",
    confirm: str = "",
) -> dict:
    """
    One-shot landlord workflow (UI-equivalent package):
      1) Create room (or reuse existing same name if use_existing…)
      2) Ensure inventory items
      3) Create DRAFT lease (deposit defaults half rent if omitted)
      4) Invite tenant if email given
      5) create_condition_inspection if create_inspection and tenant exists

    Prefer this over many separate tools for “add Room G + lease + invite + inspection”.
    """
    from rentium.properties.models import Property
    from rentium.rama import domain_actions as actions

    room_name = (room_name or "").strip()
    if not room_name:
        return {"error": "room_name is required."}

    rent_s = (total_rent or asking_rent or "").strip() or "0"
    inv = (inventory_items or "").strip()
    start = (start_date or "").strip()
    end = (end_date or "").strip()
    email = (tenant_email or "").strip()
    tname = (tenant_name or "").strip() or email.split("@")[0]

    existing = list(
        Property.objects.filter(landlord=landlord, name__iexact=room_name).order_by(
            "created_at",
        ),
    )
    from .resolve import _candidate_row

    plan = {
        "workflow": "setup_room_tenancy",
        "room_name": room_name,
        "address": address,
        "city": city,
        "group_name": group_name or None,
        "inventory_items": _parse_item_names(inv),
        "lease": {
            "start_date": start or None,
            "end_date": end or None,
            # No end date = month-to-month, which is how landlords mean it.
            "month_to_month": not end,
            "total_rent": rent_s,
            "security_deposit": security_deposit or "half month if omitted",
            "pet_deposit": pet_deposit or "0",
            "cleaning_fee": cleaning_fee or "0",
            "smoking_allowed": _truthy(smoking_allowed),
            "pets_allowed": _truthy(pets_allowed),
            "special_terms": (special_terms or "")[:200] or None,
            "type_hint": "Standard Roommate Agreement for ROOM listings",
        },
        "tenant": {"name": tname, "email": email} if email else None,
        "create_inspection": _truthy(create_inspection) and bool(email),
        "existing_same_name": [_candidate_row(p) for p in existing],
        "will_reuse_existing": bool(existing) and _truthy(use_existing_if_name_matches),
        "steps": [
            "create_or_reuse_property",
            "ensure_inventory",
            "create_lease" if start else "skip_lease_no_start_date",
            "invite_tenant" if email else "skip_invite",
            "create_condition_inspection"
            if (_truthy(create_inspection) and email)
            else "skip_inspection",
        ],
    }
    if not start:
        plan["warnings"] = [
            "start_date missing — will create room (+ inventory) only; pass start_date for lease.",
        ]

    # Smart gating (before preview): don't silently produce a half-done setup
    # when the landlord clearly intended a tenancy.
    #  - a tenant was named but no start date → ask for the start date
    #  - a lease will be created but no rent was given anywhere → ask for rent
    # An explicit total_rent="0" is respected (free room).
    if email and not start:
        return _ask_for(
            f"What date should the tenancy for {room_name} start? (YYYY-MM-DD)",
            "When they answer, call setup_room_tenancy again with the same "
            "details plus start_date=<the date>.",
        )
    if start and not (total_rent or asking_rent or "").strip():
        return _ask_for(
            f"What's the monthly rent for {room_name}?",
            "When they answer, call setup_room_tenancy again with the same "
            "details plus total_rent=<the amount>.",
        )

    if not _confirmed(confirm):
        return _preview(
            "setup_room_tenancy",
            plan,
            "Runs full room setup in one confirm. Prefer this for multi-step room requests.",
        )

    results: dict = {"workflow": "setup_room_tenancy", "steps_done": []}
    prop = None

    if existing and _truthy(use_existing_if_name_matches):
        # Prefer grouped if group_name set
        if group_name.strip():
            gmatch = [p for p in existing if p.group and group_name.lower() in p.group.name.lower()]
            prop = gmatch[0] if gmatch else existing[-1]
        else:
            prop = existing[-1]
        results["steps_done"].append(
            {"reuse_property": True, "property_id": str(prop.pk), "name": prop.name},
        )
        if group_name.strip() and not prop.group_id:
            assign_property_to_group(
                landlord,
                property_query=str(prop.pk),
                group_name=group_name,
                confirm="yes",
            )
            prop.refresh_from_db()
            results["steps_done"].append({"assigned_group": group_name})
    else:
        created = create_property(
            landlord,
            name=room_name,
            address=address,
            city=city,
            province=province,
            property_category="ROOM",
            room_type="PRIVATE",
            group_name=group_name,
            asking_rent=asking_rent or rent_s,
            inventory_items=inv,
            description="",
            confirm="yes",
        )
        if created.get("error") or created.get("needs_confirm"):
            return created
        results["steps_done"].append({"create_property": created})
        prop, err = _resolve_property(landlord, created["property"]["id"])
        if err:
            return _prop_err(err)

    # Inventory if still missing
    if inv:
        have = {n.lower() for n in prop.inventory_items.values_list("name", flat=True)}
        missing = [n for n in _parse_item_names(inv) if n.lower() not in have]
        if missing:
            inv_r = actions.bulk_add_inventory(
                landlord,
                property_query=str(prop.pk),
                items=", ".join(missing),
                confirm="yes",
            )
            results["steps_done"].append({"inventory": inv_r})

    if not start:
        results["property_id"] = str(prop.pk)
        results["done"] = True
        results["note"] = "Room ready; pass start_date to also create lease."
        return results

    lease_r = create_lease(
        landlord,
        property_query=str(prop.pk),
        start_date=start,
        end_date=end,
        # No end date given = month-to-month (never a doomed fixed-term).
        is_month_to_month="0" if end else "1",
        total_rent=rent_s,
        security_deposit=security_deposit,
        pet_deposit=pet_deposit,
        cleaning_fee=cleaning_fee,
        smoking_allowed=smoking_allowed,
        pets_allowed=pets_allowed,
        special_terms=special_terms,
        confirm="yes",
    )
    results["steps_done"].append({"create_lease": lease_r})
    if lease_r.get("error"):
        results["partial"] = True
        return results

    lease_number = lease_r.get("lease", {}).get("lease_number", "")
    if email:
        inv_t = actions.invite_tenant_to_lease(
            landlord,
            property_query=str(prop.pk),
            lease_number=lease_number,
            name=tname,
            email=email,
            confirm="yes",
        )
        results["steps_done"].append({"invite_tenant": inv_t})
        if inv_t.get("error") or inv_t.get("needs_confirm"):
            results["partial"] = True
            results["invite_error"] = inv_t
            return results

        if _truthy(create_inspection):
            insp = actions.create_condition_inspection(
                landlord,
                property_query=str(prop.pk),
                lease_number=lease_number,
                tenant_email=email,
                confirm="yes",
            )
            results["steps_done"].append({"create_condition_inspection": insp})

    results["done"] = True
    results["property_id"] = str(prop.pk)
    results["property_name"] = prop.name
    results["lease_number"] = lease_number
    results["next_ui"] = [
        "Add photos on the property page",
        "Download lease PDF from lease page after signatures",
        "Landlord sign when ready",
    ]
    return results


def _raise_tenant_claim(landlord, wo):
    """Post the damage claim for an already-costed, tenant-chargeable job.

    Shared by completion and by attributing blame AFTER the fact. Late
    attribution is the common case — the landlord fixes the shower, closes the
    job, and only later works out which tenant broke it — and COMPLETED is
    terminal, so without this the claim could never be raised at all.

    Idempotent on the work order, so attributing twice cannot double-charge.
    """
    from rentium.leases.models import Lease
    from rentium.ledger.models import EntryType
    from rentium.ledger.services import post_charge

    if wo.cost is None or not wo.tenant_chargeable or not wo.responsible_tenant_id:
        return None

    # The claim has to sit on the LEASE whose deposit it may be set against,
    # or it never shows up in deposit_position and the landlord discovers the
    # gap at move-out. A shared-space job carries no lease of its own, so fall
    # back to the responsible tenant's live lease with this landlord.
    lease = wo.lease
    if lease is None:
        lease = (
            Lease.objects.filter(
                landlord=landlord,
                lease_tenants__tenant=wo.responsible_tenant,
                status__in=("DRAFT", "PENDING", "ACTIVE"),
            )
            .order_by("-start_date")
            .first()
        )

    claim, _made = post_charge(
        landlord=landlord,
        tenant=wo.responsible_tenant,
        lease=lease,
        property=wo.property,
        holding=(
            wo.property.holding
            if wo.property_id
            else (wo.unit.holding if wo.unit_id else None)
        ),
        amount=wo.cost,
        due_date=_dt.date.today(),
        entry_type=EntryType.FEE_CHARGE,
        description=f"Damage recovery: {wo.title} ({wo.place_name})",
        work_order=wo,
        idempotency_key=f"woclaim:{wo.pk}",
        created_by=landlord.user,
        metadata={"damage_claim": True, "work_order": str(wo.pk)},
    )
    return str(getattr(claim, "pk", "") or "")


def _resolve_tenant_profile(landlord, query: str):
    """(TenantProfile, error) for a tenant in this landlord's portfolio.

    Never guesses between people — attributing damage to the wrong tenant is
    not a mistake a landlord discovers before it has cost somebody money.
    """
    from django.db.models import Q

    from rentium.leases.models import LeaseTenant

    q = (query or "").strip()
    if not q:
        return None, {"error": "Say which tenant."}

    hits = (
        LeaseTenant.objects.filter(lease__landlord=landlord, tenant__isnull=False)
        .filter(
            Q(invited_name__icontains=q)
            | Q(invited_email__icontains=q)
            | Q(tenant__user__name__icontains=q)
            | Q(tenant__user__email__icontains=q),
        )
        .select_related("tenant__user")
    )
    people = {lt.tenant_id: lt.tenant for lt in hits}
    if not people:
        return None, {"error": f"No tenant matching {q!r} in your portfolio."}
    if len(people) > 1:
        return None, {
            "error": f"Several tenants match {q!r} — which one?",
            "candidates": [p.user.name for p in people.values()],
        }
    return next(iter(people.values())), None


def attribute_work_order(
    landlord,
    *,
    work_order_id: str = "",
    title_query: str = "",
    tenant: str = "",
    chargeable: str = "",
    confirm: str = "",
) -> dict:
    """Record who caused a repair, and whether they are being charged for it."""
    wo, err = _resolve_work_order(
        landlord, work_order_id=work_order_id, title_query=title_query,
    )
    if err:
        return _prop_err(err)

    person = None
    if (tenant or "").strip():
        person, perr = _resolve_tenant_profile(landlord, tenant)
        if perr:
            return perr

    wants_charge = _truthy(chargeable)
    if wants_charge and person is None and wo.responsible_tenant_id is None:
        return {
            "error": "Say which tenant is being charged before marking it chargeable.",
        }

    target = person or wo.responsible_tenant
    preview = {
        "work_order": wo.title,
        "place": wo.place_name,
        "responsible_tenant": target.user.name if target else None,
        "chargeable_to_tenant": wants_charge,
        "cost_on_file": str(wo.cost) if wo.cost is not None else None,
    }
    if wants_charge:
        preview["what_this_does"] = (
            "Raises a claim the tenant owes once the job is completed with a "
            "cost. It does NOT deduct from their deposit — that needs their "
            "written agreement, or an RTB application within 15 days of the "
            "tenancy ending."
        )
    if not _confirmed(confirm):
        return _preview(
            "attribute_work_order",
            preview,
            "Records responsibility for this repair.",
        )

    fields = ["updated_at"]
    if person is not None:
        wo.responsible_tenant = person
        fields.append("responsible_tenant")
    if str(chargeable).strip():
        wo.tenant_chargeable = wants_charge
        fields.append("tenant_chargeable")
    wo.save(update_fields=fields)

    # Already finished and costed? Raise the claim now — otherwise blaming a
    # tenant after the job closed would never produce one, because COMPLETED
    # is terminal and the claim rides on completion.
    claim_id = None
    if wo.status == wo.Status.COMPLETED:
        claim_id = _raise_tenant_claim(landlord, wo)
    return {
        "updated": True,
        **preview,
        "tenant_claim_id": claim_id,
        "tenant_claim_note": (
            (
                f"Claimed {wo.cost} from {target.user.name}. A debt they owe — "
                "NOT a deposit deduction. Keeping deposit money needs their "
                "written agreement, or an RTB application within 15 days of "
                "the tenancy ending."
            )
            if claim_id
            else None
        ),
    }


def deposit_position(landlord, *, lease_number: str = "") -> dict:
    """Deposit held on a lease vs what is claimed against it, and the deadline."""
    from rentium.ledger.services import deposit_position as _position

    lease, err = _resolve_lease(landlord, lease_number=lease_number)
    if err:
        return _prop_err(err)
    out = _position(landlord, lease=lease)
    out["lease"] = lease.lease_number
    return out


def tenant_statement(landlord, *, tenant: str, lease_number: str = "") -> dict:
    """Everything one tenant owes and has paid, in one place."""
    from rentium.ledger.services import tenant_statement as _statement

    person, err = _resolve_tenant_profile(landlord, tenant)
    if err:
        return err
    lease = None
    if (lease_number or "").strip():
        lease, lerr = _resolve_lease(landlord, lease_number=lease_number)
        if lerr:
            return _prop_err(lerr)
    return _statement(landlord, tenant=person, lease=lease)
