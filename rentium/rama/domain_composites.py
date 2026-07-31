"""
First-class composite write tools — one preview, one confirm, one transaction
where the product already has a multi-step API workflow.

These call the same application services / model paths the REST views use
(create_lease_record, MoveOutRequest, split_utility_bill, schedule_viewing,
RentAdjustment + apply_adjustment_to_ledger, build_inspection). They do not
invent parallel business rules.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
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
# renew_lease — mirrors LeaseViewSet.renew
# ---------------------------------------------------------------------------


def renew_lease(
    landlord,
    *,
    lease_number: str = "",
    property_query: str = "",
    start_date: str = "",
    end_date: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    is_month_to_month: str = "",
    copy_tenants: str = "1",
    confirm: str = "",
) -> dict:
    """Renew an active/finalized lease: mark the old one RENEWED and create a
    new DRAFT lease linked via previous_lease, optionally copying roster."""
    from rentium.leases.models import Lease, LeaseTenant
    from rentium.leases.services import create_lease_record

    old, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)

    if old.status in (
        Lease.LeaseStatus.DRAFT,
        Lease.LeaseStatus.PENDING_SIGNATURES,
    ):
        return {
            "error": (
                f"Cannot renew a lease that is not yet active or finalized "
                f"(status={old.status}). Use update_lease / adjust_lease instead."
            ),
        }
    if old.status == Lease.LeaseStatus.RENEWED:
        return {
            "error": (
                f"Lease {old.lease_number} is already RENEWED. "
                "Find the successor via list_leases."
            ),
        }
    if not old.property_id and not old.group_id:
        return {"error": "Lease has no listing or group to renew onto."}

    try:
        if (start_date or "").strip():
            start = _parse_date(start_date, "start_date")
        elif old.end_date:
            start = old.end_date + timedelta(days=1)
        else:
            start = timezone.now().date()

        mtm = (
            _truthy(is_month_to_month)
            if (is_month_to_month or "").strip()
            else bool(old.is_month_to_month)
        )
        end = None
        if not mtm:
            if (end_date or "").strip():
                end = _parse_date(end_date, "end_date")
            elif old.end_date and old.start_date:
                span = (old.end_date - old.start_date).days
                end = start + timedelta(days=max(span, 1))
            else:
                return {
                    "error": (
                        "Fixed-term renewals need end_date (YYYY-MM-DD), or pass "
                        "is_month_to_month=yes."
                    ),
                }
            if end <= start:
                return {"error": "end_date must be after start_date."}

        rent = (
            _money(total_rent)
            if (total_rent or "").strip()
            else Decimal(old.total_rent or 0)
        )
        deposit = (
            _money(security_deposit)
            if (security_deposit or "").strip()
            else Decimal(old.security_deposit or 0)
        )
    except ValueError as exc:
        return {"error": str(exc)}

    do_copy = _truthy(copy_tenants) if (copy_tenants or "").strip() != "" else True
    tenant_preview = []
    if do_copy:
        for lt in old.lease_tenants.filter(declined=False):
            tenant_preview.append(
                {
                    "name": lt.display_name,
                    "email": (lt.invited_email or getattr(
                        getattr(lt.tenant, "user", None), "email", ""
                    ) or ""),
                    "rent_amount": str(lt.rent_amount or ""),
                }
            )

    place = (
        old.property.name
        if old.property_id
        else (old.group.name if old.group_id else "")
    )
    preview = {
        "old_lease_number": old.lease_number,
        "old_status": old.status,
        "old_becomes": Lease.LeaseStatus.RENEWED,
        "property": place,
        "new_start_date": str(start),
        "new_end_date": str(end) if end else None,
        "is_month_to_month": mtm,
        "total_rent": str(rent),
        "security_deposit": str(deposit),
        "copy_tenants": do_copy,
        "tenants_to_copy": tenant_preview or None,
        "side_effects": [
            f"Mark {old.lease_number} as RENEWED",
            "Create a new DRAFT lease linked via previous_lease",
            "Copy unsigned tenant invites when copy_tenants=yes",
        ],
    }
    if not _confirmed(confirm):
        return _preview(
            "renew_lease",
            preview,
            "Renews like the UI Renew button: old→RENEWED, new DRAFT created.",
        )

    with transaction.atomic():
        old.status = Lease.LeaseStatus.RENEWED
        old.save(update_fields=["status", "updated_at"])

        values = {
            "property": old.property if old.property_id else None,
            "group": old.group if old.group_id else None,
            "lease_type": old.lease_type,
            "status": Lease.LeaseStatus.DRAFT,
            "start_date": start,
            "end_date": end,
            "is_month_to_month": mtm,
            "move_in_date": start,
            "total_rent": rent,
            "security_deposit": deposit,
            "pet_deposit": old.pet_deposit,
            "cleaning_fee": old.cleaning_fee,
            "pets_allowed": old.pets_allowed,
            "smoking_allowed": old.smoking_allowed,
            "special_terms": old.special_terms or "",
            "etransfer_email": old.etransfer_email or "",
            "bills_included": old.bills_included or {},
            "previous_lease": old,
            "common_space_shared_with": list(old.common_space_shared_with or []),
        }
        try:
            new_lease = create_lease_record(landlord=landlord, values=values)
        except ValidationError as exc:
            return _validation_error_payload(exc)

        copied = 0
        if do_copy:
            for old_lt in old.lease_tenants.filter(declined=False):
                already = (
                    new_lease.lease_tenants.filter(tenant=old_lt.tenant).exists()
                    if old_lt.tenant_id
                    else new_lease.lease_tenants.filter(
                        invited_email__iexact=old_lt.invited_email
                    ).exists()
                )
                if already:
                    continue
                LeaseTenant.objects.create(
                    lease=new_lease,
                    tenant=old_lt.tenant,
                    invited_email=(
                        old_lt.invited_email if not old_lt.tenant_id else ""
                    ),
                    invited_name=old_lt.invited_name,
                    invited_phone=old_lt.invited_phone,
                    rent_amount=old_lt.rent_amount,
                    room=old_lt.room,
                    is_primary_tenant=old_lt.is_primary_tenant,
                    cleaning_fee=old_lt.cleaning_fee,
                    cleaning_fee_paid=False,
                    has_signed=False,
                )
                copied += 1

    return {
        "renewed": True,
        "old_lease_number": old.lease_number,
        "old_status": Lease.LeaseStatus.RENEWED,
        "new_lease": {
            "id": str(new_lease.pk),
            "lease_number": new_lease.lease_number,
            "status": new_lease.status,
            "start_date": str(new_lease.start_date),
            "end_date": str(new_lease.end_date) if new_lease.end_date else None,
            "total_rent": str(new_lease.total_rent),
            "property": place,
        },
        "tenants_copied": copied,
        "message": (
            f"Renewed {old.lease_number} → new DRAFT {new_lease.lease_number} "
            f"for {place} starting {start}."
        ),
        "next_steps": [
            "Invite/confirm tenants on the new lease if needed",
            "landlord_sign_lease once rent is allocated",
            "create_condition_inspection for the new term if desired",
        ],
    }


# ---------------------------------------------------------------------------
# settle_moveout — mirrors MoveOutViewSet.create (landlord) + settle_deposit
# ---------------------------------------------------------------------------


def settle_moveout(
    landlord,
    *,
    lease_number: str = "",
    property_query: str = "",
    requested_end_date: str = "",
    kind: str = "MUTUAL_AGREEMENT",
    reason: str = "",
    rent_handling: str = "NONE",
    moveout_id: str = "",
    forwarding_address: str = "",
    forwarding_address_received_on: str = "",
    deposit_settlement: str = "",
    tenant_agreement_signed_on: str = "",
    rtb_file_number: str = "",
    confirm: str = "",
) -> dict:
    """End a tenancy and/or record deposit settlement (UI move-out flow).

    Without moveout_id: create a landlord move-out request
    (LANDLORD_NOTICE auto-applies when date is valid; MUTUAL_AGREEMENT waits
    for the tenant). With moveout_id or after create: record forwarding
    address / deposit settlement evidence.
    """
    from rentium.leases.api.moveout_views import ENDABLE_STATUSES
    from rentium.leases.models import Lease, MoveOutRequest
    from rentium.leases.tenancy_rules import earliest_landlord_end_date
    from rentium.leases.tenancy_rules import rules_for_lease
    from rentium.leases.tenancy_rules import rules_payload

    existing = None
    mid = (moveout_id or "").strip()
    if mid:
        existing = (
            MoveOutRequest.objects.filter(pk=mid, lease__landlord=landlord)
            .select_related("lease", "lease__property")
            .first()
        )
        if not existing:
            return {"error": f"No move-out request {mid!r}."}
        lease = existing.lease
    else:
        lease, err = _resolve_lease(
            landlord, property_query=property_query, lease_number=lease_number,
        )
        if err:
            return _prop_err(err)

    place = (
        lease.property.name
        if lease.property_id
        else (lease.group.name if lease.group_id else "")
    )

    creating = existing is None
    settling = bool(
        (forwarding_address or "").strip()
        or (forwarding_address_received_on or "").strip()
        or (deposit_settlement or "").strip()
        or (rtb_file_number or "").strip()
    )

    if not creating and not settling:
        return {
            "error": (
                "Pass requested_end_date to open a move-out, and/or deposit "
                "settlement fields (forwarding_address, deposit_settlement, …)."
            ),
        }

    kind_s = (kind or "MUTUAL_AGREEMENT").strip().upper()
    handling = (rent_handling or "NONE").strip().upper()
    end = None
    if creating:
        if lease.status not in ENDABLE_STATUSES:
            return {
                "error": (
                    f"Only a live tenancy can be ended this way "
                    f"(status={lease.status})."
                ),
            }
        if not (requested_end_date or "").strip():
            return {"error": "requested_end_date is required (YYYY-MM-DD)."}
        try:
            end = _parse_date(requested_end_date, "requested_end_date")
        except ValueError as exc:
            return {"error": str(exc)}
        if end < date.today():
            return {"error": "The end date cannot be in the past."}
        if lease.moveout_requests.filter(
            status=MoveOutRequest.Status.PENDING
        ).exists():
            return {
                "error": (
                    "There is already a pending move-out on this lease. "
                    "Resolve or cancel it first, or pass moveout_id to settle deposit."
                ),
            }
        if kind_s not in (
            MoveOutRequest.Kind.LANDLORD_NOTICE,
            MoveOutRequest.Kind.MUTUAL_AGREEMENT,
        ):
            return {
                "error": "kind must be LANDLORD_NOTICE or MUTUAL_AGREEMENT.",
            }
        if handling not in MoveOutRequest.RentHandling.values:
            return {"error": f"Invalid rent_handling {rent_handling!r}."}

        if kind_s == MoveOutRequest.Kind.LANDLORD_NOTICE:
            earliest = earliest_landlord_end_date(lease)
            if end < earliest:
                rules = rules_for_lease(lease)
                return {
                    "error": (
                        f"With notice served today, earliest end is "
                        f"{earliest.isoformat()}. For an earlier date use "
                        f"kind=MUTUAL_AGREEMENT ({rules.mutual_agreement_form})."
                    ),
                    "earliest_end_date": earliest.isoformat(),
                }

    settlement_code = (deposit_settlement or "").strip().upper()
    if settlement_code:
        valid = {c for c, _ in MoveOutRequest.DepositSettlement.choices}
        if settlement_code not in valid:
            return {
                "error": (
                    f"deposit_settlement must be one of {sorted(valid)} "
                    f"(got {deposit_settlement!r})."
                ),
            }
        if settlement_code == MoveOutRequest.DepositSettlement.TENANT_AGREED:
            if not (tenant_agreement_signed_on or "").strip():
                return {
                    "error": (
                        "TENANT_AGREED settlement needs "
                        "tenant_agreement_signed_on (YYYY-MM-DD)."
                    ),
                }
        if settlement_code == MoveOutRequest.DepositSettlement.RTB_APPLIED:
            if not (rtb_file_number or "").strip():
                return {"error": "RTB settlement needs rtb_file_number."}

    preview = {
        "lease_number": lease.lease_number,
        "property": place,
        "creating_moveout": creating,
        "kind": kind_s if creating else (existing.kind if existing else None),
        "requested_end_date": str(end) if end else None,
        "rent_handling": handling if creating else None,
        "reason": (reason or "")[:200] or None,
        "settling_deposit": settling,
        "forwarding_address": (forwarding_address or "")[:80] or None,
        "deposit_settlement": settlement_code or None,
        "existing_moveout_id": str(existing.pk) if existing else None,
    }
    if not _confirmed(confirm):
        return _preview(
            "settle_moveout",
            preview,
            "Creates/accepts landlord move-out and/or records deposit settlement.",
        )

    result: dict = {"lease_number": lease.lease_number, "property": place}

    if creating:
        rules = rules_for_lease(lease)
        snapshot = rules_payload(lease)
        mo = MoveOutRequest.objects.create(
            lease=lease,
            initiated_by=MoveOutRequest.InitiatedBy.LANDLORD,
            kind=kind_s,
            requested_end_date=end,
            reason=(reason or "").strip(),
            form_type=(
                rules.mutual_agreement_form
                if kind_s == MoveOutRequest.Kind.MUTUAL_AGREEMENT
                else ""
            ),
            rent_handling=handling,
            rules_snapshot=snapshot,
        )
        mo.sign(as_landlord=True)
        if kind_s == MoveOutRequest.Kind.LANDLORD_NOTICE:
            mo.accept()
            result["applied"] = True
            result["status"] = mo.status
        else:
            mo.save()
            mo._publish("lease.moveout_requested")
            result["applied"] = False
            result["status"] = mo.status
            result["note"] = "Mutual agreement pending tenant countersignature."
        existing = mo
        result["moveout_id"] = str(mo.pk)
        result["kind"] = mo.kind
        result["requested_end_date"] = str(mo.requested_end_date)
        result["effective_end_date"] = (
            str(mo.effective_end_date) if mo.effective_end_date else None
        )

    if settling and existing is not None:
        fields = ["updated_at"]
        if (forwarding_address or "").strip():
            existing.forwarding_address = (forwarding_address or "")[:2000]
            fields.append("forwarding_address")
        if (forwarding_address_received_on or "").strip():
            try:
                existing.forwarding_address_received_on = _parse_date(
                    forwarding_address_received_on,
                    "forwarding_address_received_on",
                )
            except ValueError as exc:
                return {"error": str(exc)}
            fields.append("forwarding_address_received_on")
        if settlement_code:
            existing.deposit_settlement = settlement_code
            fields.append("deposit_settlement")
            if settlement_code == MoveOutRequest.DepositSettlement.TENANT_AGREED:
                try:
                    existing.tenant_agreement_signed_on = _parse_date(
                        tenant_agreement_signed_on,
                        "tenant_agreement_signed_on",
                    )
                except ValueError as exc:
                    return {"error": str(exc)}
                fields.append("tenant_agreement_signed_on")
            if settlement_code == MoveOutRequest.DepositSettlement.RTB_APPLIED:
                existing.rtb_file_number = (rtb_file_number or "")[:50]
                fields.append("rtb_file_number")
        if (rtb_file_number or "").strip() and "rtb_file_number" not in fields:
            existing.rtb_file_number = (rtb_file_number or "")[:50]
            fields.append("rtb_file_number")
        existing.save(update_fields=list(dict.fromkeys(fields)))
        result["deposit_settlement"] = existing.deposit_settlement
        result["forwarding_address"] = existing.forwarding_address or None
        result["moveout_id"] = str(existing.pk)

    result["settled"] = True
    result["message"] = (
        f"Move-out recorded for lease {lease.lease_number} ({place})"
        + (
            f", deposit={result.get('deposit_settlement')}"
            if result.get("deposit_settlement")
            else ""
        )
        + "."
    )
    return result


# ---------------------------------------------------------------------------
# complete_inspection_package — create + fill GOOD + optional landlord sign
# ---------------------------------------------------------------------------


def complete_inspection_package(
    landlord,
    *,
    lease_number: str = "",
    property_query: str = "",
    tenant_email: str = "",
    fill_move_in_good: str = "1",
    landlord_signature_name: str = "",
    start_move_out: str = "0",
    move_out_date: str = "",
    confirm: str = "",
) -> dict:
    """Package a condition inspection for a lease: create if missing, fill
    empty move-in codes as GOOD, optionally record landlord move-in signature,
    optionally open the move-out pass. Does NOT forge tenant signatures."""
    from rentium.leases.inspection_services import InspectionError
    from rentium.leases.inspection_services import record_signature
    from rentium.leases.inspection_services import start_move_out as start_mo
    from rentium.leases.inspections import ConditionCode, ConditionInspection
    from rentium.leases.inspections import InspectionItem
    from rentium.leases.inspections import InspectionPass

    # Reuse create_condition_inspection for tenant resolution + build_inspection
    from .domain_actions import create_condition_inspection as create_insp

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)

    place = (
        lease.property.name
        if lease.property_id
        else (lease.group.name if lease.group_id else "")
    )
    do_fill = _truthy(fill_move_in_good) if fill_move_in_good != "" else True
    do_start_mo = _truthy(start_move_out)
    sig_name = (landlord_signature_name or "").strip()
    if not sig_name:
        u = getattr(landlord, "user", None)
        if u is not None:
            sig_name = (getattr(u, "get_full_name", lambda: "")() or "").strip()
            if not sig_name:
                sig_name = (getattr(u, "name", None) or getattr(u, "email", "") or "")[
                    :120
                ]

    existing = (
        ConditionInspection.objects.filter(lease=lease)
        .order_by("-created_at")
        .first()
    )
    preview = {
        "lease_number": lease.lease_number,
        "property": place,
        "inspection_exists": bool(existing),
        "inspection_id": str(existing.pk) if existing else None,
        "inspection_status": existing.status if existing else None,
        "will_create": existing is None,
        "fill_move_in_good": do_fill,
        "landlord_signature_name": sig_name or None,
        "start_move_out": do_start_mo,
        "move_out_date": (move_out_date or "").strip() or None,
        "note": "Tenant signatures are never auto-applied.",
    }
    if not _confirmed(confirm):
        return _preview(
            "complete_inspection_package",
            preview,
            "Creates/fills condition inspection package (RTB-style).",
        )

    steps: list[str] = []
    insp = existing
    if insp is None:
        created = create_insp(
            landlord,
            property_query=property_query,
            lease_number=lease_number or lease.lease_number,
            tenant_email=tenant_email,
            confirm="yes",
        )
        if created.get("error"):
            return created
        insp_id = (created.get("inspection") or {}).get("id")
        insp = ConditionInspection.objects.filter(pk=insp_id).first()
        if not insp:
            return {"error": "Inspection create reported success but row missing."}
        steps.append("created_inspection")
    else:
        steps.append("reused_inspection")

    filled = 0
    if do_fill and not insp.pass_is_locked(InspectionPass.MOVE_IN):
        items = list(insp.items.all())
        to_update = []
        for item in items:
            changed = False
            if not item.move_in_condition_code:
                item.move_in_condition_code = ConditionCode.GOOD
                changed = True
            if not item.move_in_cleanliness_code:
                item.move_in_cleanliness_code = ConditionCode.GOOD
                changed = True
            if changed:
                to_update.append(item)
        if to_update:
            InspectionItem.objects.bulk_update(
                to_update,
                ["move_in_condition_code", "move_in_cleanliness_code", "updated_at"],
            )
            filled = len(to_update)
            steps.append(f"filled_move_in_good:{filled}")

    signed = False
    if (landlord_signature_name or "").strip() or sig_name:
        name = (landlord_signature_name or "").strip() or sig_name
        try:
            if not insp.pass_is_locked(InspectionPass.MOVE_IN):
                record_signature(
                    insp,
                    pass_name=InspectionPass.MOVE_IN,
                    role="LANDLORD",
                    signature_name=name[:200],
                )
                signed = True
                steps.append("landlord_signed_move_in")
                insp.refresh_from_db()
        except InspectionError as exc:
            steps.append(f"sign_skipped:{exc}")
        except Exception as exc:  # noqa: BLE001
            steps.append(f"sign_error:{exc}")

    move_out_started = False
    if do_start_mo:
        mo_date = None
        if (move_out_date or "").strip():
            try:
                mo_date = _parse_date(move_out_date, "move_out_date")
            except ValueError as exc:
                return {"error": str(exc)}
        try:
            start_mo(insp, move_out_date=mo_date)
            move_out_started = True
            steps.append("started_move_out")
            insp.refresh_from_db()
        except InspectionError as exc:
            steps.append(f"move_out_skipped:{exc}")
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Could not start move-out: {exc}"}

    return {
        "completed": True,
        "inspection": {
            "id": str(insp.pk),
            "status": insp.status,
            "lease_number": lease.lease_number,
            "property": place,
            "item_count": insp.items.count(),
            "items_filled_good": filled,
            "landlord_signed_move_in": signed,
            "move_out_started": move_out_started,
        },
        "steps": steps,
        "message": (
            f"Condition inspection package for lease {lease.lease_number} "
            f"({place}): status={insp.status}, filled={filled}."
        ),
        "ui": "Lease → Condition Inspections (not Calendar).",
    }


# ---------------------------------------------------------------------------
# apply_rent_adjustment — mirrors RentAdjustmentViewSet.perform_create
# ---------------------------------------------------------------------------


def apply_rent_adjustment(
    landlord,
    *,
    lease_number: str = "",
    property_query: str = "",
    tenant_email: str = "",
    adjustment_type: str = "DISCOUNT",
    amount: str = "",
    calculation_method: str = "FLAT_AMOUNT",
    reason: str = "",
    effective_date: str = "",
    end_date: str = "",
    is_recurring: str = "0",
    confirm: str = "",
) -> dict:
    """Record a rent discount/increase/other on a lease tenant and reconcile
    open rent charges (same as UI rent-adjustments)."""
    from rentium.leases.models import RentAdjustment
    from rentium.ledger.billing import apply_adjustment_to_ledger

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)

    lts = list(
        lease.lease_tenants.filter(declined=False).select_related("tenant__user")
    )
    if not lts:
        return {"error": "No tenants on this lease to adjust."}

    def _email(lt) -> str:
        if lt.invited_email:
            return (lt.invited_email or "").strip().lower()
        u = getattr(getattr(lt, "tenant", None), "user", None)
        return ((getattr(u, "email", None) or "") if u else "").strip().lower()

    chosen = None
    email_q = (tenant_email or "").strip().lower()
    if email_q:
        for lt in lts:
            if email_q == _email(lt) or email_q in _email(lt):
                chosen = lt
                break
        if not chosen:
            return {"error": f"No tenant matching {tenant_email!r} on this lease."}
    elif len(lts) == 1:
        chosen = lts[0]
    else:
        chosen = next((lt for lt in lts if lt.is_primary_tenant), None)
        if not chosen:
            return {
                "error": (
                    "Multiple tenants — pass tenant_email to pick who gets "
                    f"the adjustment. On lease: {[ _email(lt) or lt.display_name for lt in lts ]}."
                ),
            }

    adj_type = (adjustment_type or "DISCOUNT").strip().upper()
    if adj_type not in RentAdjustment.AdjustmentType.values:
        return {
            "error": (
                f"adjustment_type must be one of "
                f"{list(RentAdjustment.AdjustmentType.values)}."
            ),
        }
    method = (calculation_method or "FLAT_AMOUNT").strip().upper()
    if method not in RentAdjustment.CalculationMethod.values:
        return {
            "error": (
                f"calculation_method must be one of "
                f"{list(RentAdjustment.CalculationMethod.values)}."
            ),
        }
    if not (amount or "").strip() and method != RentAdjustment.CalculationMethod.EXACT_NIGHTLY:
        return {"error": "amount is required (dollars for FLAT, percent for PERCENTAGE)."}
    try:
        amt = _money(amount or "0")
        eff = (
            _parse_date(effective_date, "effective_date")
            if (effective_date or "").strip()
            else timezone.now().date()
        )
        end = (
            _parse_date(end_date, "end_date")
            if (end_date or "").strip()
            else None
        )
    except ValueError as exc:
        return {"error": str(exc)}

    recurring = _truthy(is_recurring)
    place = lease.property.name if lease.property_id else ""
    preview = {
        "lease_number": lease.lease_number,
        "property": place,
        "tenant": chosen.display_name,
        "tenant_email": _email(chosen) or None,
        "base_rent": str(chosen.rent_amount or lease.total_rent),
        "adjustment_type": adj_type,
        "calculation_method": method,
        "amount": str(amt),
        "effective_date": str(eff),
        "end_date": str(end) if end else None,
        "is_recurring": recurring,
        "reason": (reason or "")[:200] or None,
        "side_effects": [
            "Create RentAdjustment row",
            "Reconcile unpaid future rent charges on ACTIVE leases",
        ],
    }
    if not _confirmed(confirm):
        return _preview(
            "apply_rent_adjustment",
            preview,
            "Records rent adjustment and reconciles ledger charges.",
        )

    try:
        adjustment = RentAdjustment.objects.create(
            lease_tenant=chosen,
            adjustment_type=adj_type,
            calculation_method=method,
            amount=amt,
            reason=(reason or "")[:2000],
            effective_date=eff,
            end_date=end,
            is_recurring=recurring,
            created_by=landlord,
        )
    except ValidationError as exc:
        return _validation_error_payload(exc)

    ledger_result = {}
    if lease.status == lease.LeaseStatus.ACTIVE:
        try:
            ledger_result = apply_adjustment_to_ledger(chosen, adjustment)
        except Exception as exc:  # noqa: BLE001
            ledger_result = {"warning": f"Ledger reconcile deferred: {exc}"}

    return {
        "applied": True,
        "adjustment": {
            "id": str(adjustment.pk),
            "type": adjustment.adjustment_type,
            "amount": str(adjustment.amount),
            "method": adjustment.calculation_method,
            "effective_date": str(adjustment.effective_date),
            "end_date": str(adjustment.end_date) if adjustment.end_date else None,
            "is_recurring": adjustment.is_recurring,
        },
        "lease_number": lease.lease_number,
        "tenant": chosen.display_name,
        "ledger": ledger_result,
        "message": (
            f"Applied {adj_type} of {amt} on lease {lease.lease_number} "
            f"for {chosen.display_name} from {eff}."
        ),
    }


# ---------------------------------------------------------------------------
# record_utility_bill — mirrors ledger utility_bill_view
# ---------------------------------------------------------------------------


def record_utility_bill(
    landlord,
    *,
    lease_number: str = "",
    property_query: str = "",
    total_amount: str = "",
    period_start: str = "",
    period_end: str = "",
    description: str = "Utility bill",
    bill_key: str = "",
    due_date: str = "",
    record_landlord_expense: str = "0",
    vendor: str = "",
    confirm: str = "",
) -> dict:
    """Split a utility bill onto a lease (tenant share per bills_included)
    and optionally book the landlord's full expense — same as
    POST /api/ledger/utility-bills/."""
    from rentium.ledger.billing import split_utility_bill

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number,
    )
    if err:
        return _prop_err(err)

    if not (total_amount or "").strip():
        return {"error": "total_amount is required."}
    if not (period_start or "").strip() or not (period_end or "").strip():
        return {"error": "period_start and period_end are required (YYYY-MM-DD)."}

    key = (bill_key or "").strip() or None
    if key and key not in (lease.bills_included or {}):
        return {
            "error": (
                f"bill_key {key!r} isn't configured on this lease. "
                f"Configured: {list((lease.bills_included or {}).keys()) or 'none'}."
            ),
        }

    try:
        total = _money(total_amount)
        start = _parse_date(period_start, "period_start")
        end = _parse_date(period_end, "period_end")
        due = (
            _parse_date(due_date, "due_date")
            if (due_date or "").strip()
            else end
        )
    except ValueError as exc:
        return {"error": str(exc)}
    if end < start:
        return {"error": "period_end must be on or after period_start."}
    if total <= 0:
        return {"error": "total_amount must be positive."}

    place = lease.property.name if lease.property_id else ""
    do_expense = _truthy(record_landlord_expense)
    preview = {
        "lease_number": lease.lease_number,
        "property": place,
        "total_amount": str(total),
        "period_start": str(start),
        "period_end": str(end),
        "due_date": str(due),
        "description": (description or "Utility bill")[:120],
        "bill_key": key,
        "bills_included_on_lease": lease.bills_included or {},
        "record_landlord_expense": do_expense,
        "vendor": (vendor or "")[:80] or None,
    }
    if not _confirmed(confirm):
        return _preview(
            "record_utility_bill",
            preview,
            "Posts utility charge(s) and optional landlord expense.",
        )

    try:
        entries = split_utility_bill(
            lease=lease,
            total_amount=total,
            period_start=start,
            period_end=end,
            description=(description or "Utility bill").strip() or "Utility bill",
            due_date=due,
            record_landlord_expense=do_expense,
            expense_vendor=(vendor or "")[:200],
            bill_key=key,
            created_by=landlord.user,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not post utility bill: {exc}"}

    rows = []
    for e in entries:
        rows.append(
            {
                "id": str(e.pk),
                "entry_type": e.entry_type,
                "amount": str(e.amount),
                "due_date": str(e.due_date) if e.due_date else None,
                "description": e.description,
            }
        )

    return {
        "recorded": True,
        "lease_number": lease.lease_number,
        "property": place,
        "entries": rows,
        "count": len(rows),
        "message": (
            f"Recorded utility bill ${total} for lease {lease.lease_number} "
            f"({start} – {end}): {len(rows)} ledger entr"
            f"{'y' if len(rows) == 1 else 'ies'}."
        ),
    }


# ---------------------------------------------------------------------------
# convert_inquiry_to_viewing — mirrors InquiryViewSet.to_appointment
# ---------------------------------------------------------------------------


def convert_inquiry_to_viewing(
    landlord,
    *,
    inquiry_id: str = "",
    name_query: str = "",
    when: str = "",
    confirm: str = "",
) -> dict:
    """Turn a showcase inquiry into a scheduled viewing, carrying contact
    details — same as the UI 'to appointment' action. Uses schedule_viewing
    service so notifications fire."""
    from zoneinfo import ZoneInfo

    from rentium.appointments.services import notification_receipt
    from rentium.appointments.services import schedule_viewing as schedule_svc
    from rentium.rama.links import url_for_path
    from rentium.showcase.models import Inquiry

    inq = None
    iid = (inquiry_id or "").strip()
    if iid:
        inq = (
            Inquiry.objects.filter(pk=iid, landlord=landlord)
            .select_related("property")
            .first()
        )
        if not inq:
            return {"error": f"No inquiry {iid!r}."}
    else:
        q = (name_query or "").strip()
        if not q:
            return {"error": "Pass inquiry_id or name_query."}
        qs = (
            Inquiry.objects.filter(landlord=landlord)
            .exclude(status__in=[Inquiry.Status.ARCHIVED, Inquiry.Status.SPAM])
            .filter(name__icontains=q)
            .select_related("property")
            .order_by("-created_at")
        )
        n = qs.count()
        if n == 0:
            return {"error": f"No inquiry matching {name_query!r}."}
        if n > 1:
            return {
                "error": (
                    f"Multiple inquiries match {name_query!r}. Pass inquiry_id."
                ),
                "matches": [
                    {
                        "id": str(i.pk),
                        "name": i.name,
                        "property": i.property.name if i.property_id else "",
                        "status": i.status,
                    }
                    for i in qs[:8]
                ],
            }
        inq = qs.first()

    if inq.appointment_id:
        return {
            "error": (
                f"Inquiry already linked to appointment {inq.appointment_id}. "
                "Use reschedule_viewing if the time needs to change."
            ),
            "appointment_id": str(inq.appointment_id),
        }

    when_s = (when or "").strip()
    if not when_s:
        return {
            "error": "when is required (e.g. 2026-08-05 14:00).",
            "inquiry": {
                "id": str(inq.pk),
                "name": inq.name,
                "email": inq.email,
                "property": inq.property.name if inq.property_id else "",
            },
        }

    tz = ZoneInfo("America/Vancouver")
    starts = None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            naive = datetime.strptime(when_s.replace("Z", "")[:19], fmt)
            if fmt == "%Y-%m-%d":
                naive = datetime.combine(naive.date(), time(14, 0))
            starts = naive.replace(tzinfo=tz)
            break
        except ValueError:
            continue
    if starts is None:
        return {"error": f"Could not parse when={when!r}. Use YYYY-MM-DD HH:MM."}

    prop = inq.property
    if not prop:
        return {"error": "Inquiry has no listing attached."}

    preview = {
        "inquiry_id": str(inq.pk),
        "name": inq.name,
        "email": inq.email,
        "phone": str(inq.phone or ""),
        "property": prop.name,
        "starts_at": starts.isoformat(),
        "will_mark_inquiry": "REPLIED",
        "notes": f"From inquiry: {(inq.message or '')[:120]}",
    }
    if not _confirmed(confirm):
        return _preview(
            "convert_inquiry_to_viewing",
            preview,
            "Schedules a viewing from the inquiry and marks it replied.",
        )

    try:
        appt = schedule_svc(
            landlord=landlord,
            property_obj=prop,
            starts_at=starts,
            contact_name=(inq.name or "")[:200],
            contact_email=(inq.email or "")[:150],
            contact_phone=str(inq.phone or "")[:30],
            notes=f"From inquiry: {(inq.message or '')[:500]}",
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not create viewing: {exc}"}

    inq.appointment = appt
    inq.mark_replied()
    inq.save(update_fields=["appointment", "status", "responded_at"])

    receipt = notification_receipt(appt)
    calendar_link = url_for_path("/dashboard/calendar")
    status_link = url_for_path(f"/viewing/status/{appt.public_token}")

    return {
        "converted": True,
        "inquiry_id": str(inq.pk),
        "inquiry_status": inq.status,
        "appointment": {
            "id": str(appt.pk),
            "property": prop.name,
            "starts_at": starts.isoformat(),
            "status": appt.status,
            "contact_name": appt.contact_name,
            "contact_email": appt.contact_email,
        },
        "notified": receipt,
        "calendar_link": calendar_link,
        "prospect_status_link": status_link,
        "message": (
            f"Converted inquiry from {inq.name} into a viewing on "
            f"{starts.strftime('%Y-%m-%d %H:%M')} for {prop.name}."
        ),
    }
