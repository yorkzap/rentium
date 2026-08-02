# inspection_services.py
"""
Business logic for condition inspections — creation/prefill, pass
completion (write-back + suggestion flagging + events), and the
suggestion -> work-order pipeline. Views stay thin and call these.

All operations here are transactional and idempotent where it matters:
build_inspection() refuses duplicates via the DB constraints, write-back is
a plain upsert, and pass completion is guarded by status.
"""

import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .inspections import (
    ATTENTION_CODES,
    AreaConditionState,
    ConditionCode,
    ConditionInspection,
    InspectionItem,
    InspectionKeyRow,
    InspectionPass,
    InspectionTemplate,
)
from .models import Lease, LeaseTenant

logger = logging.getLogger(__name__)


class InspectionError(Exception):
    """Raised for business-rule violations; views translate to 400s."""


# ------------------------------------------------------ template resolution
def resolve_template(lease: Lease) -> InspectionTemplate:
    """
    Province-driven template selection (decision D2: the property's province
    picks the template; lease types are NOT forked per province). Falls back
    to GENERIC, which the BC seed also registers so there's always one.
    """
    prop = lease.property
    if prop is None and lease.group_id:
        prop = lease.group.grouped_properties.first()
    province_raw = (prop.province if prop else "") or ""
    province = "GENERIC"
    lowered = province_raw.strip().lower()
    if lowered in ("bc", "british columbia"):
        province = "BC"
    elif lowered in ("sk", "saskatchewan"):
        province = "SK"

    template = (
        InspectionTemplate.objects.filter(province=province, is_active=True)
        .order_by("-version")
        .first()
    )
    if not template:
        template = (
            InspectionTemplate.objects.filter(province="GENERIC", is_active=True)
            .order_by("-version")
            .first()
        )
    if not template:
        raise InspectionError(
            "No inspection template is seeded. Run: "
            "python manage.py seed_inspection_templates"
        )
    return template


# ------------------------------------------------- section <-> area matching
# Maps RTB-27 section names to keywords found in area names (the areas
# seeded by properties/areas.py: "Kitchen", "Bathroom", "Living Room"...).
# Matching is best-effort by design: an unmatched section still appears on
# the report unbound — the paper form doesn't require physical links, we
# just prefer them so prefill/write-back/suggestions can work.
SECTION_AREA_KEYWORDS = {
    "Entry": ("entry", "hall", "foyer"),
    "Kitchen": ("kitchen",),
    "Living Room": ("living",),
    "Dining Room": ("dining",),
    "Stairwell and Hall": ("stair", "hall"),
    "Main Bathroom": ("bathroom", "washroom", "bath"),
    "Bedroom": ("bedroom", "room"),
    "Exterior": ("exterior", "balcony", "patio", "garden", "yard"),
    "Utility Room": ("utility", "laundry"),
    "Garage or Parking Area": ("garage", "parking"),
    "Basement": ("basement",),
    "Storage": ("storage",),
}

DEFAULT_KEY_ROWS = (
    "Building entrance keys",
    "Rental unit entrance main locks",
    "Rental unit deadbolt",
    "Parking remote control",
)


def _match_area(section: str, areas) -> object | None:
    """First Area whose name contains one of the section's keywords."""
    # Longest keyword tuple key that prefixes the section (handles
    # "Bedroom (Room A)" style dynamic sections mapping to "Bedroom").
    keywords = None
    for key, words in SECTION_AREA_KEYWORDS.items():
        if section == key or section.startswith(key):
            keywords = words
            break
    if not keywords:
        return None
    for area in areas:
        name = (getattr(area, "name", "") or "").lower()
        if any(word in name for word in keywords):
            return area
    return None


def _current_area_condition(area) -> tuple[str, str]:
    """(condition_code, note) — GOOD/'' when never assessed (by design:
    nothing about condition is required at property-creation time)."""
    state = getattr(area, "condition_state", None) if area else None
    if state:
        return state.condition, state.note
    return ConditionCode.GOOD, ""


_INVENTORY_TO_INSPECTION = {
    # InventoryItem.ItemCondition -> ConditionCode
    "NEW": ConditionCode.GOOD,
    "GOOD": ConditionCode.GOOD,
    "FAIR": ConditionCode.FAIR,
    "POOR": ConditionCode.POOR,
    "DAMAGED": ConditionCode.DAMAGED,
    "MISSING": ConditionCode.MISSING,
}

_INSPECTION_TO_INVENTORY = {
    # ConditionCode -> InventoryItem.ItemCondition (SCRATCHED/BROKEN fold
    # into DAMAGED — inventory's vocabulary is coarser).
    ConditionCode.GOOD: "GOOD",
    ConditionCode.FAIR: "FAIR",
    ConditionCode.POOR: "POOR",
    ConditionCode.MISSING: "MISSING",
    ConditionCode.DAMAGED: "DAMAGED",
    ConditionCode.SCRATCHED: "DAMAGED",
    ConditionCode.BROKEN: "DAMAGED",
}


# ------------------------------------------------------------------ builder
@transaction.atomic
def build_inspection(
    *, lease: Lease, lease_tenant: LeaseTenant | None = None, created_by=None
) -> ConditionInspection:
    """
    Create the inspection document: copy template rows, inject inventory,
    prefill the move-in column from persistent condition state, seed the
    standard key rows, publish inspection.created.

    Scope (decision D3):
      - complete-unit lease  -> lease_tenant=None, all template sections,
        areas = the unit's own layout + anything on the listing
      - room/group lease     -> lease_tenant required; sections limited to
        the tenant's room + the shared areas their room touches, resolved
        by the SAME areas_for_tenant_room() maintenance uses.
    """
    from rentium.properties.areas import areas_for_tenant_room
    from rentium.properties.models import PropertyArea

    is_room_scope = lease.group_id is not None or (
        lease.property_id and lease.property.property_category == "ROOM"
    )
    if is_room_scope and lease_tenant is None:
        raise InspectionError(
            "Room/group leases need one inspection per tenant — pass lease_tenant."
        )
    if lease_tenant and lease_tenant.lease_id != lease.pk:
        raise InspectionError("That lease tenant is not on this lease.")

    template = resolve_template(lease)

    # ---- resolve visible areas + the room (if any) ----
    room = None
    if is_room_scope:
        room = lease_tenant.room or (
            lease.property if lease.property_id else None
        )
        if room is not None:
            visible_areas = list(areas_for_tenant_room(room))
        else:
            visible_areas = []
    else:
        # Whole-unit lease: the inspection covers the unit's internal layout
        # (its named bedrooms, bathrooms, kitchen) as well as anything recorded
        # against the listing itself.
        from django.db.models import Q as _Q

        scope = _Q(property=lease.property)
        if lease.property_id and lease.property.unit_id:
            scope |= _Q(unit_id=lease.property.unit_id)
        visible_areas = list(PropertyArea.objects.filter(scope).distinct())

    # ---- which template sections apply? ----
    template_items = list(template.items.all().order_by("sort_order"))
    all_sections = []
    for ti in template_items:
        if ti.section not in all_sections:
            all_sections.append(ti.section)

    if is_room_scope:
        # Their bedroom section always applies; any other section applies
        # only if it matched one of their visible (shared) areas.
        sections_to_use = []
        for section in all_sections:
            if section.startswith("Bedroom"):
                # Collapse the form's two bedroom sections into one for the
                # tenant's own room.
                if not any(s.startswith("Bedroom") for s in sections_to_use):
                    sections_to_use.append(section)
                continue
            if _match_area(section, visible_areas) is not None or section in (
                "Entry",
            ):
                sections_to_use.append(section)
        # Visible areas with no matching template section still deserve a
        # row each (e.g. a seeded "Backyard" area) — collected below.
    else:
        sections_to_use = all_sections

    try:
        inspection = ConditionInspection.objects.create(
            lease=lease,
            lease_tenant=lease_tenant,
            template=template,
            possession_date=lease.move_in_date or lease.start_date,
            created_by=created_by,
        )
    except IntegrityError:
        raise InspectionError(
            "An inspection already exists for this tenancy — open it instead."
        )

    # ---- copy template rows, bind areas, prefill move-in column ----
    items, sort = [], 0
    matched_area_ids = set()
    for section in sections_to_use:
        section_label = section
        if is_room_scope and section.startswith("Bedroom") and room is not None:
            section_label = f"Bedroom — {room.name}"
        section_area = None if section.startswith("Bedroom") else _match_area(
            section, visible_areas
        )
        if section_area is not None:
            matched_area_ids.add(section_area.pk)
        cond, note = _current_area_condition(section_area)
        for ti in (t for t in template_items if t.section == section):
            sort += 10
            items.append(
                InspectionItem(
                    inspection=inspection,
                    section=section_label,
                    label=ti.label,
                    sort_order=sort,
                    area=section_area,
                    move_in_condition_code=cond
                    if cond != ConditionCode.GOOD
                    else ConditionCode.GOOD,
                    move_in_comment=note,
                )
            )

    # ---- visible areas that matched no section: one generic row each ----
    for area in visible_areas:
        if area.pk in matched_area_ids:
            continue
        sort += 10
        cond, note = _current_area_condition(area)
        items.append(
            InspectionItem(
                inspection=inspection,
                section=getattr(area, "name", "Other Area") or "Other Area",
                label="General condition",
                sort_order=sort,
                area=area,
                move_in_condition_code=cond,
                move_in_comment=note,
            )
        )

    # ---- inject inventory (decision: reuse existing inventory, tagged) ----
    from rentium.properties.models import InventoryItem as PrivateItem
    from rentium.properties.models import SharedInventoryItem

    inv_qs = []
    if is_room_scope and room is not None:
        inv_qs = list(PrivateItem.objects.filter(property=room))
        if room.group_id:
            inv_qs += list(SharedInventoryItem.objects.filter(group=room.group))
    elif lease.property_id:
        inv_qs = list(PrivateItem.objects.filter(property=lease.property))

    for inv in inv_qs:
        sort += 10
        prefill = _INVENTORY_TO_INSPECTION.get(inv.condition or "GOOD", ConditionCode.GOOD)
        shared = isinstance(inv, SharedInventoryItem)
        items.append(
            InspectionItem(
                inspection=inspection,
                section="Inventory / Furnishings",
                label=f"{inv.name}" + (f" ×{inv.quantity}" if inv.quantity > 1 else ""),
                sort_order=sort,
                inventory_item=None if shared else inv,
                shared_inventory_item=inv if shared else None,
                move_in_condition_code=prefill,
                move_in_comment=inv.location_description or "",
            )
        )

    InspectionItem.objects.bulk_create(items)

    InspectionKeyRow.objects.bulk_create(
        InspectionKeyRow(
            inspection=inspection, key_type=key_type, sort_order=i * 10
        )
        for i, key_type in enumerate(DEFAULT_KEY_ROWS)
    )

    _publish(
        "inspection.created",
        inspection,
        {"possession_date": str(inspection.possession_date or "")},
    )
    return inspection


# ----------------------------------------------------------------- signing
@transaction.atomic
def record_signature(
    inspection: ConditionInspection,
    *,
    pass_name: str,
    role: str,  # "LANDLORD" | "TENANT"
    signature_name: str,
    agrees: bool | None = None,
    disagreement_reason: str = "",
) -> ConditionInspection:
    """
    Record one party's click-to-sign for one pass, then complete the pass if
    both parties have now signed. Per RTB-27, a tenant who DISAGREES still
    signs — the disagreement + reasons are recorded (Boxes Y / 1), the
    document advances either way.
    """
    if pass_name not in InspectionPass.values:
        raise InspectionError("Unknown pass.")
    name = (signature_name or "").strip()
    if not name:
        raise InspectionError("A typed signature name is required.")

    now = timezone.now()
    is_move_in = pass_name == InspectionPass.MOVE_IN

    # Guard: correct pass for the current status.
    if is_move_in and inspection.status not in (
        ConditionInspection.Status.MOVE_IN_IN_PROGRESS,
    ):
        raise InspectionError("The move-in pass is not open for signatures.")
    if not is_move_in and inspection.status not in (
        ConditionInspection.Status.MOVE_OUT_IN_PROGRESS,
    ):
        raise InspectionError("The move-out pass is not open for signatures.")

    fields = []
    if role == "LANDLORD":
        prefix = "landlord"
    elif role == "TENANT":
        prefix = "tenant"
        if agrees is None:
            raise InspectionError(
                "Tenant signature requires agrees=true/false (RTB-27 Box Y / Box 1)."
            )
        if agrees is False and not (disagreement_reason or "").strip():
            raise InspectionError("Please give a reason for disagreeing.")
        suffix = "move_in" if is_move_in else "move_out"
        setattr(inspection, f"tenant_agrees_{suffix}", agrees)
        setattr(
            inspection,
            f"tenant_disagreement_{suffix}",
            (disagreement_reason or "").strip(),
        )
        fields += [f"tenant_agrees_{suffix}", f"tenant_disagreement_{suffix}"]
    else:
        raise InspectionError("Unknown signer role.")

    ts_field = f"{prefix}_signed_{'move_in' if is_move_in else 'move_out'}_at"
    name_field = f"{prefix}_{'move_in' if is_move_in else 'move_out'}_signature_name"
    if getattr(inspection, ts_field):
        raise InspectionError("This party has already signed this pass.")
    setattr(inspection, ts_field, now)
    setattr(inspection, name_field, name)
    fields += [ts_field, name_field, "updated_at"]
    inspection.save(update_fields=fields)

    # Landlord signed first -> nudge the tenant to review & sign.
    if role == "LANDLORD" and not getattr(
        inspection, f"tenant_signed_{'move_in' if is_move_in else 'move_out'}_at"
    ):
        _publish("inspection.awaiting_signature", inspection, {"pass": pass_name})

    _complete_pass_if_fully_signed(inspection, pass_name)
    return inspection


def _complete_pass_if_fully_signed(inspection, pass_name: str) -> None:
    is_move_in = pass_name == InspectionPass.MOVE_IN
    if is_move_in and inspection.move_in_fully_signed:
        inspection.status = ConditionInspection.Status.MOVE_IN_SIGNED
        if not inspection.move_in_inspection_date:
            inspection.move_in_inspection_date = timezone.now().date()
        inspection.save(
            update_fields=["status", "move_in_inspection_date", "updated_at"]
        )
        apply_condition_writeback(inspection, InspectionPass.MOVE_IN)
        flagged = flag_attention_items(inspection, InspectionPass.MOVE_IN)
        _publish(
            "inspection.completed",
            inspection,
            {"pass": pass_name, "disputed": inspection.disputed_move_in},
        )
        if flagged:
            _publish("inspection.suggestions", inspection, {"count": flagged})
    elif not is_move_in and inspection.move_out_fully_signed:
        inspection.status = ConditionInspection.Status.COMPLETED
        if not inspection.move_out_inspection_date:
            inspection.move_out_inspection_date = timezone.now().date()
        inspection.save(
            update_fields=["status", "move_out_inspection_date", "updated_at"]
        )
        apply_condition_writeback(inspection, InspectionPass.MOVE_OUT)
        flagged = flag_attention_items(inspection, InspectionPass.MOVE_OUT)
        _publish(
            "inspection.completed",
            inspection,
            {"pass": pass_name, "disputed": inspection.disputed_move_out},
        )
        if flagged:
            _publish("inspection.suggestions", inspection, {"count": flagged})


# --------------------------------------------------------------- move-out
@transaction.atomic
def start_move_out(inspection: ConditionInspection, *, move_out_date=None):
    """Open the End-of-tenancy pass, prefilling the end column from the
    CURRENT condition state (which may have improved since move-in if
    repairs happened) — the walkthrough then confirms or overrides."""
    if inspection.status != ConditionInspection.Status.MOVE_IN_SIGNED:
        raise InspectionError(
            "Move-out can only start once the move-in pass is fully signed."
        )
    inspection.status = ConditionInspection.Status.MOVE_OUT_IN_PROGRESS
    if move_out_date:
        inspection.move_out_date = move_out_date
    inspection.save(update_fields=["status", "move_out_date", "updated_at"])

    for item in inspection.items.all():
        cond = None
        if item.area_id:
            cond, _note = _current_area_condition(item.area)
        elif item.inventory_item_id and item.inventory_item.condition:
            cond = _INVENTORY_TO_INSPECTION.get(item.inventory_item.condition)
        elif item.shared_inventory_item_id and item.shared_inventory_item.condition:
            cond = _INVENTORY_TO_INSPECTION.get(item.shared_inventory_item.condition)
        item.move_out_condition_code = cond or item.move_in_condition_code
        item.save(update_fields=["move_out_condition_code", "updated_at"])
    return inspection


# ------------------------------------------------------------- write-back
def apply_condition_writeback(inspection, pass_name: str) -> None:
    """
    Persist the signed pass's codes onto the physical world so the NEXT
    tenancy's inspection prefills truthfully (decision D4): Areas via the
    AreaConditionState satellite, inventory via its existing condition
    field. Best-effort per item — a write-back failure must never block a
    signed legal document.
    """
    is_move_in = pass_name == InspectionPass.MOVE_IN
    for item in inspection.items.all():
        code = (
            item.move_in_condition_code if is_move_in else item.move_out_condition_code
        )
        comment = item.move_in_comment if is_move_in else item.move_out_comment
        if not code:
            continue
        try:
            if item.area_id:
                AreaConditionState.objects.update_or_create(
                    area_id=item.area_id,
                    defaults={
                        "condition": code,
                        "note": comment[:255],
                        "source_inspection": inspection,
                    },
                )
            inv = item.inventory_item or item.shared_inventory_item
            if inv is not None:
                mapped = _INSPECTION_TO_INVENTORY.get(code)
                if mapped and inv.condition != mapped:
                    inv.condition = mapped
                    inv.save(update_fields=["condition", "updated_at"])
        except Exception:
            logger.exception(
                "Condition write-back failed for inspection item %s", item.pk
            )


def flag_attention_items(inspection, pass_name: str) -> int:
    """Damage-ish codes become PENDING maintenance suggestions."""
    is_move_in = pass_name == InspectionPass.MOVE_IN
    flagged = 0
    for item in inspection.items.all():
        code = (
            item.move_in_condition_code if is_move_in else item.move_out_condition_code
        )
        if code in ATTENTION_CODES and item.suggestion_status in (
            InspectionItem.SuggestionStatus.NONE,
            InspectionItem.SuggestionStatus.DISMISSED,
        ):
            item.needs_attention = True
            item.suggestion_status = InspectionItem.SuggestionStatus.PENDING
            item.save(
                update_fields=["needs_attention", "suggestion_status", "updated_at"]
            )
            flagged += 1
    return flagged


# ---------------------------------------------------- suggestion pipeline
@transaction.atomic
def approve_suggestion(item: InspectionItem, *, user) -> object:
    """Landlord approves -> a WorkOrder is born, pre-filled and linked back.
    (origin=LANDLORD: the maintenance FSM has no INSPECTION origin yet —
    adding one is a one-line choice + migration if you want the analytics.)"""
    from rentium.maintenance.models import WorkOrder

    if item.suggestion_status != InspectionItem.SuggestionStatus.PENDING:
        raise InspectionError("This item has no pending suggestion.")

    inspection = item.inspection
    lease = inspection.lease
    prop = None
    if item.area_id and getattr(item.area, "property_id", None):
        prop = item.area.property
    if prop is None:
        prop = lease.property or (
            inspection.lease_tenant.room if inspection.lease_tenant else None
        )

    # Damage in SHARED space belongs to the unit, not to whichever room the
    # lease happens to name. Without this, approving a suggestion on a shared
    # washroom raised "Couldn't resolve a property" and the damage — already
    # photographed and recorded — could not become a job at all.
    unit = None
    if prop is None:
        unit = getattr(item.area, "unit", None) if item.area_id else None
        if unit is None and lease.property_id:
            unit = lease.property.unit
    if prop is None and unit is None:
        raise InspectionError(
            "Couldn't resolve a property or unit for the work order — set one "
            "manually."
        )

    # Who was living there. This is evidence, not a verdict: `tenant_chargeable`
    # stays False because "damage" versus "fair wear and tear" is the
    # landlord's judgement, and getting it wrong charges somebody wrongly.
    responsible = None
    if inspection.lease_tenant_id and inspection.lease_tenant.tenant_id:
        responsible = inspection.lease_tenant.tenant
    elif lease.lease_tenants.filter(tenant__isnull=False).count() == 1:
        responsible = lease.lease_tenants.filter(tenant__isnull=False).first().tenant

    code = item.latest_code()
    code_display = dict(ConditionCode.choices).get(code, code)
    comment = item.move_out_comment or item.move_in_comment
    work_order = WorkOrder.objects.create(
        property=prop,
        unit=unit,
        area=item.area,
        # Carried so a damage claim can find the lease whose deposit it may be
        # claimed against.
        lease=lease,
        responsible_tenant=responsible,
        title=f"{item.section}: {item.label} — {code_display}"[:200],
        description=(
            f"From condition inspection {inspection.pk} "
            f"(lease {inspection.lease.lease_number}). "
            f"Recorded condition: {code_display}."
            + (f" Notes: {comment}" if comment else "")
        ),
        category=WorkOrder.Category.OTHER
        if hasattr(WorkOrder, "Category")
        else "OTHER",
        priority="MEDIUM",
        origin=WorkOrder.Origin.LANDLORD,
        reported_by=user,
    )
    item.suggestion_status = InspectionItem.SuggestionStatus.APPROVED
    item.work_order = work_order
    item.save(update_fields=["suggestion_status", "work_order", "updated_at"])
    return work_order


def dismiss_suggestion(item: InspectionItem) -> None:
    """Landlord dismisses -> condition stays recorded (write-back already
    persisted it), no job is created. Exactly 'cancel for it to stay in
    that condition'."""
    if item.suggestion_status != InspectionItem.SuggestionStatus.PENDING:
        raise InspectionError("This item has no pending suggestion.")
    item.suggestion_status = InspectionItem.SuggestionStatus.DISMISSED
    item.save(update_fields=["suggestion_status", "updated_at"])


# ------------------------------------------------------------------ events
def _publish(event_type: str, inspection: ConditionInspection, extra: dict) -> None:
    """Outbox publish, never allowed to break the business operation."""
    try:
        from rentium.events.registry import publish

        lease = inspection.lease
        payload = {
            "inspection_id": str(inspection.pk),
            "lease_number": lease.lease_number,
            **extra,
        }
        if inspection.lease_tenant_id:
            payload["lease_tenant_id"] = str(inspection.lease_tenant_id)
        publish(
            event_type,
            payload,
            property_id=lease.property_id,
            lease_id=lease.pk,
        )
    except Exception:
        logger.exception("Failed to publish %s for inspection %s", event_type, inspection.pk)


def agreed_deductions(lease, balances, *, require_agreed=True) -> dict:
    """{charge_id: Decimal} — what the move-out inspections say may be kept.

    The deduction lines live on the inspection (that is the document that
    records the state the place was left in, signed by both parties), and the
    money lives in the ledger as separate deposit charges. This maps one onto
    the other.

    `require_agreed=False` gives the same figures as a PROPOSAL, for the screen
    that shows the landlord what a settlement would look like. Only the agreed
    ones may actually be posted — under the BC RTA a landlord keeps deposit
    money with the tenant's written agreement or an RTB order, and nothing else.

    A roommate lease has one inspection per tenant, so each is allocated only
    against charges that could be theirs (their own, or the household's).
    Allocation is sequential, and each pass sees what the previous ones left.
    """
    from rentium.ledger.services import allocate_deductions

    inspections = lease.inspections.all()
    if require_agreed:
        inspections = inspections.filter(deduction_agreed_at__isnull=False)

    remaining = [dict(item) for item in balances]
    allocated: dict = {}
    for inspection in inspections:
        totals = {
            kind: amount
            for kind, amount in inspection.deduction_totals().items()
            if amount > 0
        }
        if not totals:
            continue
        pool = remaining
        if inspection.lease_tenant_id:
            tenant_id = inspection.lease_tenant.tenant_id
            pool = [
                item
                for item in remaining
                if item["tenant"] is None
                or (tenant_id and item["tenant"].pk == tenant_id)
            ]
        share = allocate_deductions(pool, totals)
        for charge_id, amount in share.items():
            allocated[charge_id] = allocated.get(charge_id, Decimal("0.00")) + amount
        remaining = [
            {
                **item,
                "balance": item["balance"] - share.get(item["charge_id"], Decimal("0")),
            }
            for item in remaining
        ]
        remaining = [item for item in remaining if item["balance"] > 0]
    return allocated
