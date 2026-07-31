"""
API↔RAMA gap-close tools — landlord chat surface for REST operations that
already exist in the product but had no RAMA wrapper.

Every function calls application services / model paths used by the matching
DRF views. Confirm-gated writes only.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .domain_crud import (
    _confirmed,
    _money,
    _parse_date,
    _preview,
    _prop_err,
    _resolve_lease,
    _resolve_property,
    _truthy,
    _validation_error_payload,
)


# ---------------------------------------------------------------------------
# Ledger control surface
# ---------------------------------------------------------------------------


def _resolve_ledger_entry(landlord, entry_id: str = "", description_query: str = ""):
    from rentium.ledger.models import LedgerEntry

    eid = (entry_id or "").strip()
    if eid:
        entry = (
            LedgerEntry.objects.filter(landlord=landlord, pk=eid)
            .select_related("lease", "property", "tenant")
            .first()
        )
        if not entry:
            return None, f"No ledger entry {eid!r}."
        return entry, None
    q = (description_query or "").strip()
    if not q:
        return None, "Pass entry_id or description_query."
    qs = (
        LedgerEntry.objects.filter(landlord=landlord, description__icontains=q)
        .filter(reversed_by__isnull=True)
        .order_by("-effective_date", "-created_at")
    )
    n = qs.count()
    if n == 0:
        return None, f"No ledger entry matching {description_query!r}."
    if n > 1:
        sample = [
            {
                "id": str(e.pk),
                "type": e.entry_type,
                "amount": str(e.amount),
                "description": (e.description or "")[:80],
                "date": str(e.effective_date or e.due_date or ""),
            }
            for e in qs[:8]
        ]
        return None, {
            "error": f"Multiple entries match {description_query!r}; pass entry_id.",
            "matches": sample,
        }
    return qs.first(), None


def void_ledger_entry(
    landlord,
    *,
    entry_id: str = "",
    description_query: str = "",
    reason: str = "",
    confirm: str = "",
) -> dict:
    """Void a ledger entry via equal-and-opposite REVERSAL (append-only)."""
    from rentium.ledger import services as ledger_services

    entry, err = _resolve_ledger_entry(landlord, entry_id, description_query)
    if err:
        return err if isinstance(err, dict) else {"error": err}
    why = (reason or "").strip()
    if not why:
        return {"error": "reason is required — it goes on the audit trail."}

    preview = {
        "entry_id": str(entry.pk),
        "entry_type": entry.entry_type,
        "amount": str(entry.amount),
        "description": entry.description,
        "reason": why[:200],
        "side_effects": ["Post REVERSAL row; original row stays for audit"],
    }
    if not _confirmed(confirm):
        return _preview(
            "void_ledger_entry",
            preview,
            "Voids via reversal (never deletes the original row).",
        )
    try:
        reversal = ledger_services.void_entry(
            entry, reason=why, created_by=landlord.user
        )
    except ledger_services.LedgerError as exc:
        return {"error": str(exc)}
    return {
        "voided": True,
        "entry_id": str(entry.pk),
        "reversal_id": str(reversal.pk),
        "message": f"Voided {entry.entry_type} ${entry.amount}: {entry.description[:80]}.",
    }


def mark_ledger_paid(
    landlord,
    *,
    entry_id: str = "",
    description_query: str = "",
    paid_on: str = "",
    unmark: str = "0",
    confirm: str = "",
) -> dict:
    """Mark an expense as bank-cleared (or unmark). Only mutates paid_on."""
    from rentium.ledger import services as ledger_services

    entry, err = _resolve_ledger_entry(landlord, entry_id, description_query)
    if err:
        return err if isinstance(err, dict) else {"error": err}

    do_unmark = _truthy(unmark)
    when = None
    if not do_unmark:
        try:
            when = (
                _parse_date(paid_on, "paid_on")
                if (paid_on or "").strip()
                else date.today()
            )
        except ValueError as exc:
            return {"error": str(exc)}

    preview = {
        "entry_id": str(entry.pk),
        "entry_type": entry.entry_type,
        "amount": str(entry.amount),
        "description": entry.description,
        "action": "unmark" if do_unmark else "mark_paid",
        "paid_on": None if do_unmark else str(when),
        "current_paid_on": str(entry.paid_on) if entry.paid_on else None,
    }
    if not _confirmed(confirm):
        return _preview(
            "mark_ledger_paid",
            preview,
            "Sets or clears expense paid_on (bank clearing date only).",
        )
    try:
        if do_unmark:
            updated = ledger_services.unmark_expense_paid(entry)
        else:
            updated = ledger_services.mark_expense_paid(
                entry, paid_on=when, created_by=landlord.user
            )
    except ledger_services.LedgerError as exc:
        return {"error": str(exc)}
    return {
        "updated": True,
        "entry_id": str(updated.pk),
        "paid_on": str(updated.paid_on) if updated.paid_on else None,
        "message": (
            f"Expense ${updated.amount} marked paid on {updated.paid_on}."
            if updated.paid_on
            else f"Expense ${updated.amount} unmarked (not yet taken from bank)."
        ),
    }


def correct_ledger_entry(
    landlord,
    *,
    entry_id: str = "",
    description_query: str = "",
    amount: str = "",
    description: str = "",
    category: str = "",
    vendor: str = "",
    reason: str = "Correction",
    confirm: str = "",
) -> dict:
    """Edit a posted entry by void+repost (audit-safe correction)."""
    from rentium.ledger import services as ledger_services

    entry, err = _resolve_ledger_entry(landlord, entry_id, description_query)
    if err:
        return err if isinstance(err, dict) else {"error": err}

    changes: dict = {}
    try:
        if (amount or "").strip():
            changes["amount"] = _money(amount)
        if description != "":
            changes["description"] = (description or "")[:255]
        if (category or "").strip():
            changes["category"] = category.strip()[:40]
        if vendor != "":
            changes["vendor"] = (vendor or "")[:120]
    except ValueError as exc:
        return {"error": str(exc)}
    if not changes:
        return {"error": "Nothing to change — pass amount and/or description etc."}

    preview = {
        "entry_id": str(entry.pk),
        "from": {
            "amount": str(entry.amount),
            "description": entry.description,
            "category": entry.category,
            "vendor": entry.vendor,
        },
        "to": {k: (str(v) if not isinstance(v, str) else v) for k, v in changes.items()},
        "reason": (reason or "Correction")[:200],
    }
    if not _confirmed(confirm):
        return _preview(
            "correct_ledger_entry",
            preview,
            "Voids original and posts corrected replacement.",
        )
    try:
        replacement = ledger_services.correct_entry(
            entry,
            created_by=landlord.user,
            reason=(reason or "Correction")[:500],
            **changes,
        )
    except ledger_services.LedgerError as exc:
        return {"error": str(exc)}
    return {
        "corrected": True,
        "old_entry_id": str(entry.pk),
        "new_entry_id": str(replacement.pk),
        "amount": str(replacement.amount),
        "description": replacement.description,
        "message": f"Corrected entry → ${replacement.amount}: {replacement.description[:80]}.",
    }


def post_ledger_credit(
    landlord,
    *,
    entry_id: str = "",
    description_query: str = "",
    amount: str = "",
    reason: str = "Credit",
    confirm: str = "",
) -> dict:
    """Post a goodwill/discount credit against a charge."""
    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import CHARGE_TYPES

    entry, err = _resolve_ledger_entry(landlord, entry_id, description_query)
    if err:
        return err if isinstance(err, dict) else {"error": err}
    if entry.entry_type not in CHARGE_TYPES:
        return {"error": f"Credits apply to charges, not {entry.entry_type}."}
    if not (amount or "").strip():
        return {"error": "amount is required."}
    try:
        amt = _money(amount)
    except ValueError as exc:
        return {"error": str(exc)}
    if amt <= 0:
        return {"error": "amount must be positive."}

    preview = {
        "charge_id": str(entry.pk),
        "charge_amount": str(entry.amount),
        "charge_description": entry.description,
        "credit_amount": str(amt),
        "reason": (reason or "Credit")[:200],
    }
    if not _confirmed(confirm):
        return _preview("post_ledger_credit", preview, "Posts CREDIT settling the charge.")
    try:
        credit, _created = ledger_services.post_credit(
            charge=entry,
            amount=amt,
            reason=(reason or "Credit")[:255],
            created_by=landlord.user,
        )
    except ledger_services.LedgerError as exc:
        return {"error": str(exc)}
    return {
        "credited": True,
        "credit_id": str(credit.pk),
        "charge_id": str(entry.pk),
        "amount": str(credit.amount),
        "message": f"Credited ${amt} against charge {entry.description[:60]}.",
    }


def post_one_off_charge(
    landlord,
    *,
    lease_number: str = "",
    property_query: str = "",
    amount: str = "",
    due_date: str = "",
    description: str = "Charge",
    entry_type: str = "OTHER_CHARGE",
    tenant_email: str = "",
    confirm: str = "",
) -> dict:
    """Manual one-off charge (damage, late fee, etc.) on a lease."""
    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import CHARGE_TYPES, EntryType
    from rentium.ledger.billing import lease_is_joint

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)
    if not (amount or "").strip():
        return {"error": "amount is required."}
    try:
        amt = _money(amount)
        due = (
            _parse_date(due_date, "due_date")
            if (due_date or "").strip()
            else date.today()
        )
    except ValueError as exc:
        return {"error": str(exc)}
    if amt <= 0:
        return {"error": "amount must be positive."}

    etype = (entry_type or EntryType.OTHER_CHARGE).strip().upper()
    if etype not in CHARGE_TYPES:
        return {"error": f"entry_type must be a charge type, got {entry_type!r}."}

    tenant = None
    email_q = (tenant_email or "").strip().lower()
    if email_q:
        for lt in lease.lease_tenants.filter(declined=False).select_related(
            "tenant__user"
        ):
            em = (lt.invited_email or "").lower()
            u = getattr(getattr(lt, "tenant", None), "user", None)
            if u and (getattr(u, "email", "") or "").lower() == email_q:
                tenant = lt.tenant
                break
            if em == email_q or email_q in em:
                tenant = lt.tenant
                break
        if tenant is None and not lease_is_joint(lease):
            return {"error": f"No tenant matching {tenant_email!r}."}
    elif not lease_is_joint(lease):
        lts = list(lease.lease_tenants.filter(declined=False))
        if len(lts) == 1 and lts[0].tenant_id:
            tenant = lts[0].tenant
        elif lts:
            return {
                "error": (
                    "Split-billing lease needs tenant_email for who is charged."
                ),
            }

    place = lease.property.name if lease.property_id else ""
    preview = {
        "lease_number": lease.lease_number,
        "property": place,
        "amount": str(amt),
        "due_date": str(due),
        "description": (description or "Charge")[:120],
        "entry_type": etype,
        "tenant": getattr(getattr(tenant, "user", None), "email", None) or (
            "joint household" if tenant is None else str(tenant.pk)
        ),
    }
    if not _confirmed(confirm):
        return _preview(
            "post_one_off_charge",
            preview,
            "Posts a one-off charge on the lease ledger.",
        )
    try:
        entry, _created = ledger_services.post_charge(
            landlord=landlord,
            tenant=tenant,
            lease=lease,
            amount=amt,
            due_date=due,
            entry_type=etype,
            description=(description or "Charge").strip()[:255] or "Charge",
            created_by=landlord.user,
        )
    except ledger_services.LedgerError as exc:
        return {"error": str(exc)}
    return {
        "charged": True,
        "entry_id": str(entry.pk),
        "lease_number": lease.lease_number,
        "amount": str(entry.amount),
        "due_date": str(entry.due_date),
        "message": (
            f"Posted ${entry.amount} {etype} on lease {lease.lease_number} "
            f"due {entry.due_date}."
        ),
    }


# ---------------------------------------------------------------------------
# Inspection gap-close
# ---------------------------------------------------------------------------


def update_inspection_items(
    landlord,
    *,
    inspection_id: str = "",
    lease_number: str = "",
    items_json: str = "",
    fill_empty_move_in_good: str = "0",
    confirm: str = "",
) -> dict:
    """Bulk-update condition inspection item codes (UI items_bulk).

    items_json: JSON list of {id, move_in_condition_code?, move_out_condition_code?,
    move_in_cleanliness_code?, move_out_cleanliness_code?, move_in_comment?,
    move_out_comment?}. Or fill_empty_move_in_good=yes to set blank move-in to GOOD.
    """
    import json

    from rentium.leases.inspections import ConditionCode, ConditionInspection
    from rentium.leases.inspections import InspectionItem, InspectionPass

    insp = None
    iid = (inspection_id or "").strip()
    if iid:
        insp = (
            ConditionInspection.objects.filter(pk=iid, lease__landlord=landlord)
            .select_related("lease")
            .first()
        )
        if not insp:
            return {"error": f"No inspection {iid!r}."}
    else:
        lease, err = _resolve_lease(landlord, lease_number=lease_number)
        if err:
            return _prop_err(err)
        insp = (
            ConditionInspection.objects.filter(lease=lease)
            .order_by("-created_at")
            .first()
        )
        if not insp:
            return {
                "error": (
                    f"No inspection on lease {lease.lease_number}. "
                    "Use complete_inspection_package first."
                ),
            }

    rows = []
    raw = (items_json or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                return {"error": "items_json must be a JSON list."}
            rows = parsed
        except json.JSONDecodeError as exc:
            return {"error": f"Invalid items_json: {exc}"}
    do_fill = _truthy(fill_empty_move_in_good)
    if not rows and not do_fill:
        return {
            "error": (
                "Pass items_json=[{id, move_in_condition_code, ...}] or "
                "fill_empty_move_in_good=yes."
            ),
        }

    preview = {
        "inspection_id": str(insp.pk),
        "lease_number": insp.lease.lease_number,
        "status": insp.status,
        "item_updates": len(rows),
        "fill_empty_move_in_good": do_fill,
    }
    if not _confirmed(confirm):
        return _preview(
            "update_inspection_items",
            preview,
            "Updates condition codes on inspection items.",
        )

    move_in_locked = insp.pass_is_locked(InspectionPass.MOVE_IN)
    move_out_locked = insp.pass_is_locked(InspectionPass.MOVE_OUT)
    items_by_id = {str(i.pk): i for i in insp.items.all()}
    updated = []

    if do_fill and not move_in_locked:
        for item in items_by_id.values():
            changed = False
            if not item.move_in_condition_code:
                item.move_in_condition_code = ConditionCode.GOOD
                changed = True
            if not item.move_in_cleanliness_code:
                item.move_in_cleanliness_code = ConditionCode.GOOD
                changed = True
            if changed:
                updated.append(item)

    allowed = {
        "move_in_condition_code",
        "move_in_cleanliness_code",
        "move_in_comment",
        "move_out_condition_code",
        "move_out_cleanliness_code",
        "move_out_comment",
    }
    for row in rows:
        rid = str(row.get("id") or "")
        item = items_by_id.get(rid)
        if item is None:
            return {"error": f"Item {rid!r} is not on this inspection."}
        touched = {k for k in row if k in allowed and row[k] is not None}
        if move_in_locked and touched & {
            "move_in_condition_code",
            "move_in_cleanliness_code",
            "move_in_comment",
        }:
            return {"error": "Move-in columns are locked (pass fully signed)."}
        if move_out_locked and touched & {
            "move_out_condition_code",
            "move_out_cleanliness_code",
            "move_out_comment",
        }:
            return {"error": "Move-out columns are locked (pass fully signed)."}
        for field in touched:
            setattr(item, field, row[field])
        if item not in updated:
            updated.append(item)

    if updated:
        InspectionItem.objects.bulk_update(
            updated,
            [
                "move_in_condition_code",
                "move_in_cleanliness_code",
                "move_in_comment",
                "move_out_condition_code",
                "move_out_cleanliness_code",
                "move_out_comment",
                "updated_at",
            ],
        )
    return {
        "updated": True,
        "inspection_id": str(insp.pk),
        "items_updated": len(updated),
        "message": f"Updated {len(updated)} inspection item(s).",
    }


def approve_inspection_suggestion(
    landlord,
    *,
    item_id: str = "",
    inspection_id: str = "",
    confirm: str = "",
) -> dict:
    """Approve a pending inspection damage suggestion → creates a work order."""
    from rentium.leases.inspection_services import InspectionError, approve_suggestion
    from rentium.leases.inspections import InspectionItem

    item = (
        InspectionItem.objects.filter(
            pk=(item_id or "").strip(),
            inspection__lease__landlord=landlord,
        )
        .select_related("inspection__lease", "area")
        .first()
    )
    if not item:
        return {"error": f"No inspection item {item_id!r}."}
    if (inspection_id or "").strip() and str(item.inspection_id) != inspection_id.strip():
        return {"error": "item_id does not belong to that inspection_id."}

    preview = {
        "item_id": str(item.pk),
        "label": item.label,
        "section": item.section,
        "suggestion_status": item.suggestion_status,
        "lease_number": item.inspection.lease.lease_number,
    }
    if not _confirmed(confirm):
        return _preview(
            "approve_inspection_suggestion",
            preview,
            "Creates a work order from the inspection suggestion.",
        )
    try:
        wo = approve_suggestion(item, user=landlord.user)
    except InspectionError as exc:
        return {"error": str(exc)}
    item.refresh_from_db()
    return {
        "approved": True,
        "item_id": str(item.pk),
        "work_order_id": str(wo.pk),
        "work_order_title": getattr(wo, "title", "") or "",
        "message": f"Approved suggestion → work order {wo.pk}.",
    }


def dismiss_inspection_suggestion(
    landlord,
    *,
    item_id: str = "",
    confirm: str = "",
) -> dict:
    """Dismiss a pending inspection maintenance suggestion."""
    from rentium.leases.inspection_services import InspectionError, dismiss_suggestion
    from rentium.leases.inspections import InspectionItem

    item = (
        InspectionItem.objects.filter(
            pk=(item_id or "").strip(),
            inspection__lease__landlord=landlord,
        ).first()
    )
    if not item:
        return {"error": f"No inspection item {item_id!r}."}
    preview = {
        "item_id": str(item.pk),
        "label": item.label,
        "suggestion_status": item.suggestion_status,
    }
    if not _confirmed(confirm):
        return _preview(
            "dismiss_inspection_suggestion",
            preview,
            "Dismisses the pending suggestion without a work order.",
        )
    try:
        dismiss_suggestion(item)
    except InspectionError as exc:
        return {"error": str(exc)}
    item.refresh_from_db()
    return {
        "dismissed": True,
        "item_id": str(item.pk),
        "suggestion_status": item.suggestion_status,
    }


def mark_inspection_delivered(
    landlord,
    *,
    inspection_id: str = "",
    lease_number: str = "",
    inspection_pass: str = "MOVE_IN",
    confirm: str = "",
) -> dict:
    """Stamp that the landlord gave the tenant their inspection report copy."""
    from rentium.leases.inspections import ConditionInspection, InspectionPass

    insp = None
    iid = (inspection_id or "").strip()
    if iid:
        insp = ConditionInspection.objects.filter(
            pk=iid, lease__landlord=landlord
        ).first()
        if not insp:
            return {"error": f"No inspection {iid!r}."}
    else:
        lease, err = _resolve_lease(landlord, lease_number=lease_number)
        if err:
            return _prop_err(err)
        insp = (
            ConditionInspection.objects.filter(lease=lease)
            .order_by("-created_at")
            .first()
        )
        if not insp:
            return {"error": "No inspection found for that lease."}

    pass_name = (inspection_pass or "MOVE_IN").strip().upper()
    if pass_name not in InspectionPass.values:
        return {"error": "inspection_pass must be MOVE_IN or MOVE_OUT."}

    preview = {
        "inspection_id": str(insp.pk),
        "lease_number": insp.lease.lease_number,
        "inspection_pass": pass_name,
    }
    if not _confirmed(confirm):
        return _preview(
            "mark_inspection_delivered",
            preview,
            "Stamps the compliance delivery clock for the report copy.",
        )
    now = timezone.now()
    if pass_name == InspectionPass.MOVE_IN:
        insp.move_in_report_delivered_at = now
        insp.save(update_fields=["move_in_report_delivered_at", "updated_at"])
    else:
        insp.move_out_report_delivered_at = now
        insp.save(update_fields=["move_out_report_delivered_at", "updated_at"])
    return {
        "delivered": True,
        "inspection_id": str(insp.pk),
        "inspection_pass": pass_name,
        "delivered_at": now.isoformat(),
        "message": f"Marked {pass_name} report delivered for inspection {insp.pk}.",
    }


# ---------------------------------------------------------------------------
# Viewing cancel
# ---------------------------------------------------------------------------


def cancel_viewing(
    landlord,
    *,
    appointment_id: str = "",
    request_ref: str = "",
    property_query: str = "",
    reason: str = "",
    confirm: str = "",
) -> dict:
    """Cancel a viewing (pending or scheduled) → CANCELLED + event."""
    from rentium.appointments.models import Appointment
    from rentium.appointments.services import notification_receipt

    appt = None
    aid = (appointment_id or "").strip()
    ref = (request_ref or "").strip()
    if aid:
        appt = Appointment.objects.filter(
            pk=aid, landlord=landlord, kind=Appointment.Kind.VIEWING
        ).select_related("property").first()
    elif ref:
        # match list_viewing_requests short ref
        qs = Appointment.objects.filter(
            landlord=landlord, kind=Appointment.Kind.VIEWING
        ).select_related("property")
        for a in qs.order_by("-starts_at")[:100]:
            if str(a.pk)[:8].upper() == ref.upper() or str(a.pk) == ref:
                appt = a
                break
    elif property_query:
        prop, err = _resolve_property(landlord, property_query)
        if err:
            return _prop_err(err)
        appt = (
            Appointment.objects.filter(
                landlord=landlord,
                property=prop,
                kind=Appointment.Kind.VIEWING,
            )
            .exclude(status=Appointment.Status.CANCELLED)
            .order_by("-starts_at")
            .first()
        )
    if appt is None:
        return {
            "error": (
                "No viewing found. Pass appointment_id, request_ref, or property_query."
            ),
        }
    if appt.status == Appointment.Status.CANCELLED:
        return {
            "already_done": True,
            "appointment_id": str(appt.pk),
            "message": "Viewing is already cancelled.",
        }

    preview = {
        "appointment_id": str(appt.pk),
        "ref": str(appt.pk)[:8].upper(),
        "property": appt.property.name if appt.property_id else "",
        "starts_at": appt.starts_at.isoformat() if appt.starts_at else None,
        "status": appt.status,
        "reason": (reason or "")[:200] or None,
    }
    if not _confirmed(confirm):
        return _preview(
            "cancel_viewing",
            preview,
            "Cancels the viewing and notifies via appointment.cancelled.",
        )
    if reason:
        appt.notes = f"{appt.notes}\n[Cancelled] {reason}".strip()
        appt.save(update_fields=["notes", "updated_at"])
    appt.status = Appointment.Status.CANCELLED
    appt.save(update_fields=["status", "updated_at"])
    appt.publish_event("appointment.cancelled", cancelled_by="LANDLORD")
    return {
        "cancelled": True,
        "appointment_id": str(appt.pk),
        "status": appt.status,
        "notified": notification_receipt(appt),
        "message": (
            f"Cancelled viewing on {appt.property.name if appt.property_id else 'listing'}."
        ),
    }


# ---------------------------------------------------------------------------
# Cleaning fee + payment reminders + inquiry
# ---------------------------------------------------------------------------


def mark_cleaning_fee_paid(
    landlord,
    *,
    lease_number: str = "",
    property_query: str = "",
    tenant_email: str = "",
    confirm: str = "",
) -> dict:
    """Mark a lease tenant's cleaning fee as paid."""
    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)
    lts = list(lease.lease_tenants.filter(declined=False))
    if not lts:
        return {"error": "No tenants on this lease."}

    def _em(lt):
        if lt.invited_email:
            return lt.invited_email.lower()
        u = getattr(getattr(lt, "tenant", None), "user", None)
        return (getattr(u, "email", "") or "").lower()

    chosen = None
    q = (tenant_email or "").strip().lower()
    if q:
        for lt in lts:
            if q == _em(lt) or q in _em(lt):
                chosen = lt
                break
        if not chosen:
            return {"error": f"No tenant matching {tenant_email!r}."}
    elif len(lts) == 1:
        chosen = lts[0]
    else:
        return {
            "error": "Multiple tenants — pass tenant_email.",
            "tenants": [_em(lt) or lt.display_name for lt in lts],
        }

    if chosen.cleaning_fee_paid:
        return {
            "already_done": True,
            "message": f"Cleaning fee already marked paid for {chosen.display_name}.",
        }
    if (chosen.cleaning_fee or 0) <= 0:
        return {"error": "No cleaning fee was set for this tenant."}

    preview = {
        "lease_number": lease.lease_number,
        "tenant": chosen.display_name,
        "cleaning_fee": str(chosen.cleaning_fee),
    }
    if not _confirmed(confirm):
        return _preview(
            "mark_cleaning_fee_paid",
            preview,
            "Marks cleaning_fee_paid=True on the lease tenant.",
        )
    chosen.cleaning_fee_paid = True
    chosen.save(update_fields=["cleaning_fee_paid", "updated_at"])
    return {
        "updated": True,
        "lease_number": lease.lease_number,
        "tenant": chosen.display_name,
        "cleaning_fee": str(chosen.cleaning_fee),
        "message": f"Marked cleaning fee paid for {chosen.display_name}.",
    }


def list_payment_reminders(
    landlord, *, lease_number: str = "", pending_only: str = "1"
) -> dict:
    """List payment reminders on this portfolio."""
    from rentium.leases.models import PaymentReminder

    qs = PaymentReminder.objects.filter(
        payment__lease__landlord=landlord
    ).select_related("payment__lease", "payment")
    if (lease_number or "").strip():
        qs = qs.filter(payment__lease__lease_number__iexact=lease_number.strip())
    if _truthy(pending_only) if pending_only != "" else True:
        qs = qs.filter(is_sent=False)
    rows = []
    for r in qs.order_by("reminder_date")[:50]:
        rows.append(
            {
                "id": str(r.pk),
                "reminder_date": str(r.reminder_date),
                "is_sent": r.is_sent,
                "send_method": r.send_method,
                "lease_number": r.payment.lease.lease_number,
                "payment_amount": str(r.payment.amount_due),
                "message_template": (r.message_template or "")[:120] or None,
            }
        )
    return {"count": len(rows), "reminders": rows}


def create_payment_reminder(
    landlord,
    *,
    lease_number: str = "",
    property_query: str = "",
    reminder_date: str = "",
    message: str = "",
    send_method: str = "EMAIL",
    confirm: str = "",
) -> dict:
    """Schedule a payment reminder against the lease's next open payment."""
    from rentium.leases.models import Payment, PaymentReminder

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)
    payment = (
        Payment.objects.filter(lease=lease)
        .exclude(
            status__in=[
                Payment.PaymentStatus.COMPLETED,
                Payment.PaymentStatus.CANCELLED,
                Payment.PaymentStatus.REFUNDED,
            ]
        )
        .order_by("due_date")
        .first()
    )
    if not payment:
        return {
            "error": (
                f"No open Payment row on lease {lease.lease_number}. "
                "Modern rent uses the ledger; use charge_status / record_payment."
            ),
        }
    try:
        when = (
            _parse_date(reminder_date, "reminder_date")
            if (reminder_date or "").strip()
            else date.today()
        )
    except ValueError as exc:
        return {"error": str(exc)}
    method = (send_method or "EMAIL").strip().upper()
    if method not in ("EMAIL", "SMS", "APP"):
        return {"error": "send_method must be EMAIL, SMS, or APP."}

    preview = {
        "lease_number": lease.lease_number,
        "payment_id": str(payment.pk),
        "payment_amount": str(payment.amount_due),
        "reminder_date": str(when),
        "send_method": method,
        "message": (message or "")[:120] or None,
    }
    if not _confirmed(confirm):
        return _preview(
            "create_payment_reminder",
            preview,
            "Creates a PaymentReminder on the open payment.",
        )
    rem = PaymentReminder.objects.create(
        payment=payment,
        reminder_date=when,
        message_template=(message or "")[:2000],
        send_method=method,
    )
    return {
        "created": True,
        "reminder_id": str(rem.pk),
        "lease_number": lease.lease_number,
        "reminder_date": str(rem.reminder_date),
        "message": f"Scheduled reminder for {when} on lease {lease.lease_number}.",
    }


def mark_payment_reminder_sent(
    landlord,
    *,
    reminder_id: str = "",
    confirm: str = "",
) -> dict:
    """Mark a payment reminder as sent (records sent_date)."""
    from rentium.leases.models import PaymentReminder

    rem = (
        PaymentReminder.objects.filter(
            pk=(reminder_id or "").strip(),
            payment__lease__landlord=landlord,
        )
        .select_related("payment__lease")
        .first()
    )
    if not rem:
        return {"error": f"No reminder {reminder_id!r}."}
    if rem.is_sent:
        return {"already_done": True, "message": "Reminder already marked sent."}
    preview = {
        "reminder_id": str(rem.pk),
        "reminder_date": str(rem.reminder_date),
        "lease_number": rem.payment.lease.lease_number,
    }
    if not _confirmed(confirm):
        return _preview(
            "mark_payment_reminder_sent",
            preview,
            "Marks the reminder as sent.",
        )
    rem.is_sent = True
    rem.sent_date = timezone.now()
    rem.save(update_fields=["is_sent", "sent_date"])
    return {
        "updated": True,
        "reminder_id": str(rem.pk),
        "sent_date": rem.sent_date.isoformat(),
    }


def update_inquiry(
    landlord,
    *,
    inquiry_id: str = "",
    name_query: str = "",
    status: str = "",
    landlord_notes: str = "",
    confirm: str = "",
) -> dict:
    """Update inquiry notes or status (e.g. ARCHIVED)."""
    from rentium.showcase.models import Inquiry

    inq = None
    iid = (inquiry_id or "").strip()
    if iid:
        inq = Inquiry.objects.filter(pk=iid, landlord=landlord).first()
        if not inq:
            return {"error": f"No inquiry {iid!r}."}
    else:
        q = (name_query or "").strip()
        if not q:
            return {"error": "Pass inquiry_id or name_query."}
        qs = Inquiry.objects.filter(landlord=landlord, name__icontains=q)
        if qs.count() != 1:
            return {
                "error": f"Need exactly one inquiry matching {name_query!r} (found {qs.count()}).",
            }
        inq = qs.first()

    changes = {}
    st = (status or "").strip().upper()
    if st:
        if st not in Inquiry.Status.values:
            return {
                "error": f"status must be one of {list(Inquiry.Status.values)}.",
            }
        changes["status"] = st
    if landlord_notes != "":
        changes["landlord_notes"] = (landlord_notes or "")[:5000]
    if not changes:
        return {"error": "Pass status and/or landlord_notes."}

    preview = {
        "inquiry_id": str(inq.pk),
        "name": inq.name,
        "property": inq.property.name if inq.property_id else "",
        "current_status": inq.status,
        "changes": changes,
    }
    if not _confirmed(confirm):
        return _preview("update_inquiry", preview, "Updates inquiry status/notes.")
    for k, v in changes.items():
        setattr(inq, k, v)
    inq.save(update_fields=list(changes.keys()))
    return {
        "updated": True,
        "inquiry_id": str(inq.pk),
        "status": inq.status,
        "message": f"Updated inquiry from {inq.name}.",
    }


# ---------------------------------------------------------------------------
# Import batch commit/discard
# ---------------------------------------------------------------------------


def commit_import_batch(
    landlord,
    *,
    batch_id: str = "",
    confirm: str = "",
) -> dict:
    """Commit a DRAFT ledger import batch into real ledger rows."""
    from rentium.ledger.import_services import commit_batch
    from rentium.ledger.models import ImportBatch

    bid = (batch_id or "").strip()
    if bid:
        batch = ImportBatch.objects.filter(pk=bid, landlord=landlord).first()
    else:
        batch = (
            ImportBatch.objects.filter(
                landlord=landlord, status=ImportBatch.Status.DRAFT
            )
            .order_by("-created_at")
            .first()
        )
    if not batch:
        return {"error": "No DRAFT import batch found. Pass batch_id."}
    if batch.status != ImportBatch.Status.DRAFT:
        return {"error": f"Batch is {batch.status}, only DRAFT can be committed."}

    preview = {
        "batch_id": str(batch.pk),
        "status": batch.status,
        "label": batch.label or batch.source_filename or "",
        "side_effects": ["Posts real ledger rows from staged entries"],
    }
    if not _confirmed(confirm):
        return _preview(
            "commit_import_batch",
            preview,
            "Commits provisional import rows into the live ledger.",
        )
    try:
        result = commit_batch(batch, created_by=landlord.user)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not commit batch: {exc}"}
    return {
        "committed": True,
        "batch_id": str(batch.pk),
        "result": result if isinstance(result, dict) else {"ok": True},
        "message": f"Committed import batch {batch.pk}.",
    }


def discard_import_batch(
    landlord,
    *,
    batch_id: str = "",
    confirm: str = "",
) -> dict:
    """Discard a DRAFT ledger import batch without posting."""
    from rentium.ledger.models import ImportBatch

    bid = (batch_id or "").strip()
    if not bid:
        return {"error": "batch_id is required."}
    batch = ImportBatch.objects.filter(pk=bid, landlord=landlord).first()
    if not batch:
        return {"error": f"No import batch {bid!r}."}
    if batch.status == ImportBatch.Status.COMMITTED:
        return {
            "error": (
                "Already committed — real ledger rows are not removed by discard."
            ),
        }

    preview = {
        "batch_id": str(batch.pk),
        "status": batch.status,
        "side_effects": ["Mark batch DISCARDED; no ledger posts removed"],
    }
    if not _confirmed(confirm):
        return _preview("discard_import_batch", preview, "Discards the draft batch.")

    batch.status = ImportBatch.Status.DISCARDED
    batch.save(update_fields=["status"])
    return {
        "discarded": True,
        "batch_id": str(batch.pk),
        "message": f"Discarded import batch {batch.pk}.",
    }


# ---------------------------------------------------------------------------
# Notifications (light)
# ---------------------------------------------------------------------------


def list_notifications(landlord, *, unread_only: str = "1", limit: str = "30") -> dict:
    """List in-app notifications for this landlord user."""
    from rentium.events.models import Notification

    try:
        lim = max(1, min(100, int(limit or "30")))
    except ValueError:
        lim = 30
    qs = Notification.objects.filter(recipient=landlord.user).order_by("-created_at")
    if _truthy(unread_only) if unread_only != "" else True:
        qs = qs.filter(read_at__isnull=True)
    rows = []
    for n in qs[:lim]:
        rows.append(
            {
                "id": str(n.pk),
                "title": n.title or "",
                "body": (n.body or "")[:160],
                "read": bool(n.read_at),
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
        )
    return {"count": len(rows), "notifications": rows}


def mark_notifications_read(
    landlord,
    *,
    notification_id: str = "",
    all_unread: str = "0",
    confirm: str = "",
) -> dict:
    """Mark one or all unread notifications as read."""
    from rentium.events.models import Notification

    do_all = _truthy(all_unread)
    nid = (notification_id or "").strip()
    if not do_all and not nid:
        return {"error": "Pass notification_id or all_unread=yes."}

    preview = {
        "notification_id": nid or None,
        "all_unread": do_all,
    }
    if not _confirmed(confirm):
        return _preview(
            "mark_notifications_read",
            preview,
            "Marks notification(s) read.",
        )
    now = timezone.now()
    if do_all:
        n = Notification.objects.filter(
            recipient=landlord.user, read_at__isnull=True
        ).update(read_at=now)
        return {"updated": True, "count": n, "message": f"Marked {n} notification(s) read."}
    obj = Notification.objects.filter(pk=nid, recipient=landlord.user).first()
    if not obj:
        return {"error": f"No notification {nid!r}."}
    obj.mark_read()
    return {"updated": True, "notification_id": str(obj.pk), "count": 1}
