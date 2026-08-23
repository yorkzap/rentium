"""Canonical operational fact projections for RAMA's trusted core lanes."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from django.utils import timezone

from rentium.core.evidence import Coverage
from rentium.core.evidence import EvidenceGap
from rentium.core.evidence import Fact
from rentium.core.evidence import FactPacket
from rentium.core.evidence import FactSource

if TYPE_CHECKING:
    from datetime import date


def _fact(key, value, model, record_id, field) -> Fact:
    return Fact(
        key=key,
        value=value,
        value_type=type(value).__name__,
        source=FactSource(model=model, record_id=str(record_id), field=field),
    )


def lease_terms_and_status(lease) -> FactPacket:
    fields = (
        "lease_number",
        "status",
        "lease_type",
        "start_date",
        "end_date",
        "move_in_date",
        "move_out_date",
        "is_month_to_month",
        "total_rent",
        "rent_due_day",
        "security_deposit",
        "pet_deposit",
        "cleaning_deposit",
        "landlord_signed",
    )
    payload = {}
    fact_rows = []
    for field in fields:
        value = getattr(lease, field, None)
        if value is None:
            continue
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif field in {
            "total_rent",
            "security_deposit",
            "pet_deposit",
            "cleaning_deposit",
        }:
            value = str(value)
        payload[field] = value
        fact_rows.append(_fact(field, value, "leases.Lease", lease.pk, field))
    return FactPacket(
        subject={"type": "lease", "id": str(lease.pk), "label": lease.lease_number},
        facet="lease.terms_and_status",
        as_of=timezone.now().isoformat(),
        coverage=Coverage.COMPLETE,
        facts=tuple(fact_rows),
        metadata=payload,
    )


def property_structure_and_offerings(holding) -> FactPacket:
    facts = [
        _fact(
            "holding.name",
            holding.name,
            "properties.PropertyHolding",
            holding.pk,
            "name",
        ),
        _fact(
            "holding.address",
            holding.address,
            "properties.PropertyHolding",
            holding.pk,
            "address",
        ),
        _fact(
            "holding.city",
            holding.city,
            "properties.PropertyHolding",
            holding.pk,
            "city",
        ),
    ]
    units = []
    missing = []
    for unit in holding.units.prefetch_related("areas", "offerings").order_by("name"):
        areas = [
            {
                "id": str(area.pk),
                "name": area.name,
                "area_type": area.area_type,
                "kind": area.kind,
                "shared_with_landlord": area.shared_with_landlord,
                "is_seeded_default": area.is_seeded_default,
            }
            for area in unit.areas.order_by("name", "id")
            if not area.is_seeded_default
        ]
        offerings = [
            {
                "id": str(offering.pk),
                "name": offering.name,
                "category": offering.property_category,
                "active": offering.is_active_offering,
                "status": offering.status,
            }
            for offering in unit.offerings.order_by("name", "id")
        ]
        row = {
            "id": str(unit.pk),
            "name": unit.name,
            "unit_type": unit.unit_type,
            "rental_mode": unit.rental_mode,
            "layout_complete": unit.layout_complete,
            "missing_layout_notes": unit.missing_layout_notes,
            "areas": areas,
            "offerings": offerings,
        }
        units.append(row)
        for key, value in row.items():
            facts.append(
                _fact(
                    f"units.{unit.pk}.{key}",
                    value,
                    "properties.PropertyUnit",
                    unit.pk,
                    key,
                ),
            )
        if not unit.layout_complete:
            missing.append(
                EvidenceGap(
                    key=f"units.{unit.pk}.layout",
                    reason=unit.missing_layout_notes
                    or (
                        "This unit is explicitly marked as having incomplete "
                        "layout data."
                    ),
                    source=FactSource(
                        model="properties.PropertyUnit",
                        record_id=str(unit.pk),
                        field="layout_complete",
                    ),
                ),
            )
    coverage = Coverage.PARTIAL if missing else Coverage.COMPLETE
    return FactPacket(
        subject={
            "type": "property_holding",
            "id": str(holding.pk),
            "label": holding.name,
        },
        facet="property.structure_and_offerings",
        as_of=timezone.now().isoformat(),
        coverage=coverage,
        facts=tuple(facts),
        missing=tuple(missing),
        allowed_negative_conclusions=("no_units",)
        if coverage == Coverage.COMPLETE and not units
        else (),
        metadata={
            "holding": {
                "id": str(holding.pk),
                "name": holding.name,
                "address": holding.address,
            },
            "units": units,
        },
    )


def ledger_account_position(
    landlord, *, scope=None, as_of: date | None = None,
) -> FactPacket:
    from rentium.ledger.position import Scope
    from rentium.ledger.position import financial_position

    scope = scope or Scope.portfolio()
    position = financial_position(landlord, scope=scope, as_of=as_of)
    raw = asdict(position)
    payload = {
        key: value.isoformat()
        if hasattr(value, "isoformat")
        else str(value)
        if key
        not in {
            "open_count",
            "overdue_count",
            "income_open_count",
            "income_overdue_count",
            "scope",
        }
        else value
        for key, value in raw.items()
    }
    facts = tuple(
        _fact(
            key,
            value,
            "ledger.LedgerEntry",
            position.scope,
            f"aggregate:{key}",
        )
        for key, value in payload.items()
    )
    return FactPacket(
        subject={"type": "ledger_scope", "id": position.scope, "label": position.scope},
        facet="ledger.account_position",
        as_of=position.as_of.isoformat(),
        coverage=Coverage.COMPLETE,
        facts=facts,
        allowed_negative_conclusions=tuple(
            key
            for key in (
                "no_outstanding_now" if position.outstanding_now == 0 else "",
                "no_deposits_held" if position.deposits_held == 0 else "",
                "no_overdue_charges" if position.overdue_count == 0 else "",
            )
            if key
        ),
        metadata=payload,
    )


def maintenance_case_status(work_order) -> FactPacket:
    fields = (
        "title",
        "description",
        "status",
        "priority",
        "category",
        "origin",
        "scheduled_date",
        "completed_date",
        "cost",
        "tenant_chargeable",
        "contractor_name",
        "sla_due_at",
        "sla_breach_notified",
    )
    facts = []
    payload = {"id": str(work_order.pk), "place": work_order.place_name}
    for field in fields:
        value = getattr(work_order, field)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif field == "cost" and value is not None:
            value = str(value)
        payload[field] = value
        facts.append(
            _fact(
                field,
                value,
                "maintenance.WorkOrder",
                work_order.pk,
                field,
            ),
        )
    return FactPacket(
        subject={
            "type": "work_order",
            "id": str(work_order.pk),
            "label": work_order.title,
        },
        facet="maintenance.case_status",
        as_of=timezone.now().isoformat(),
        coverage=Coverage.COMPLETE,
        facts=tuple(facts),
        metadata=payload,
    )
