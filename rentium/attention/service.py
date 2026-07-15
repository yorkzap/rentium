"""
The Action Center: everything that currently needs the landlord's attention,
computed on read from existing models. No stored task rows — nothing to sync,
nothing to go stale. The NotificationBell announces *events*; this module
holds *state* that persists until the underlying condition is resolved.

Design notes (see docs/phase-b-spec.md in the frontend repo, and
docs/rama-architecture.md):

- Each source is its own small, side-effect-free function. That keeps them
  individually testable today and individually exposable as AI tools later.
- Province-awareness comes from leases.tenancy_rules: the rules module
  computes *requirements*, this layer only surfaces them. When another
  province lands in tenancy_rules, its items appear here with no UI change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta

# Windows mirror ledger/daily.py's delivery-reminder clocks (BC RTA: signed
# move-in report to tenant within 7 days, move-out within 15). Kept as
# module constants so the daily task and this module can be de-duplicated
# onto one helper later without behavior change.
MOVE_IN_DELIVERY_DAYS = 7
MOVE_OUT_DELIVERY_DAYS = 15
DELIVERY_WARN_DAYS = 2

STALLED_SIGNATURE_DAYS = 7
LEASE_EXPIRY_HORIZON_DAYS = 60

SEVERITY_ORDER = {"urgent": 0, "soon": 1, "info": 2}


@dataclass
class ActionItem:
    key: str          # stable id, e.g. "inspection.move_in.lease:<uuid>"
    severity: str     # "urgent" | "soon" | "info"
    title: str
    detail: str
    url: str          # dashboard deep link
    due_date: date | None
    source: str       # "inspection" | "lease" | "ledger" | "maintenance"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["due_date"] = self.due_date.isoformat() if self.due_date else None
        return d


def _active_and_pending_leases(landlord):
    from rentium.leases.models import Lease

    return Lease.objects.filter(
        landlord=landlord,
        status__in=[Lease.LeaseStatus.ACTIVE, Lease.LeaseStatus.PENDING_SIGNATURES],
    ).select_related("property")


def _place(lease) -> str:
    if lease.property:
        return lease.property.name
    if lease.group:
        return lease.group.name
    return lease.lease_number or "your property"


def _missing_move_in_inspections(landlord) -> list[ActionItem]:
    """
    BC RTA tenancies must have a move-in condition inspection (RTB-27).
    An active/signing lease under a full-RTA rulebook with no inspection
    started at all is the compliance gap most likely to cost a deposit
    claim — surface it until one exists.
    """
    from rentium.leases.tenancy_rules import rules_for_lease

    items: list[ActionItem] = []
    leases = _active_and_pending_leases(landlord).prefetch_related("inspections")
    for lease in leases:
        rules = rules_for_lease(lease)
        if not rules.rta_applies:
            continue  # exempt (e.g. shared-with-landlord) or generic rulebook
        if lease.inspections.exists():
            continue
        items.append(
            ActionItem(
                key=f"inspection.move_in.lease:{lease.pk}",
                severity="urgent",
                title="Schedule the move-in condition inspection",
                detail=f"{_place(lease)} — required by the {rules.jurisdiction} tenancy rules",
                url=f"/dashboard/leases/{lease.pk}",
                due_date=lease.start_date,
                source="inspection",
            )
        )
    return items


def _inspection_delivery_due(landlord) -> list[ActionItem]:
    """
    Signed reports must reach the tenant on a clock (7 days move-in,
    15 move-out in BC); missing it extinguishes deposit claims. Same
    computation as ledger/daily.py's reminder task, surfaced as state.
    """
    from rentium.leases.inspections import ConditionInspection

    today = date.today()
    items: list[ActionItem] = []

    qs = (
        ConditionInspection.objects.select_related("lease", "lease__property")
        .filter(lease__landlord=landlord)
        .exclude(status=ConditionInspection.Status.MOVE_IN_IN_PROGRESS)
    )
    for insp in qs:
        checks = [
            ("MOVE_IN", insp.move_in_inspection_date,
             MOVE_IN_DELIVERY_DAYS, insp.move_in_report_delivered_at),
        ]
        if insp.status == ConditionInspection.Status.COMPLETED:
            checks.append(
                ("MOVE_OUT", insp.move_out_inspection_date,
                 MOVE_OUT_DELIVERY_DAYS, insp.move_out_report_delivered_at),
            )
        for pass_name, signed_date, window_days, delivered_at in checks:
            if not signed_date or delivered_at:
                continue
            deadline = signed_date + timedelta(days=window_days)
            days_left = (deadline - today).days
            if days_left > DELIVERY_WARN_DAYS:
                continue
            overdue = days_left < 0
            label = "move-in" if pass_name == "MOVE_IN" else "move-out"
            items.append(
                ActionItem(
                    key=f"inspection.delivery.{pass_name.lower()}:{insp.pk}",
                    severity="urgent" if overdue else "soon",
                    title=(
                        f"Deliver the {label} inspection report"
                        + (" — overdue" if overdue else "")
                    ),
                    detail=(
                        f"{_place(insp.lease)} — signed report must reach the "
                        f"tenant by {deadline.isoformat()}"
                    ),
                    url=f"/dashboard/leases/{insp.lease_id}",
                    due_date=deadline,
                    source="inspection",
                )
            )
    return items


def _stalled_signatures(landlord) -> list[ActionItem]:
    from django.utils import timezone

    from rentium.leases.models import Lease

    cutoff = timezone.now() - timedelta(days=STALLED_SIGNATURE_DAYS)
    items: list[ActionItem] = []
    qs = Lease.objects.filter(
        landlord=landlord,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        updated_at__lt=cutoff,
    ).select_related("property")
    for lease in qs:
        items.append(
            ActionItem(
                key=f"lease.signatures:{lease.pk}",
                severity="soon",
                title="Lease is waiting on signatures",
                detail=f"{_place(lease)} — pending for over {STALLED_SIGNATURE_DAYS} days; nudge or re-send",
                url=f"/dashboard/leases/{lease.pk}",
                due_date=lease.start_date,
                source="lease",
            )
        )
    return items


def _expiring_leases(landlord) -> list[ActionItem]:
    from rentium.leases.models import Lease

    today = date.today()
    horizon = today + timedelta(days=LEASE_EXPIRY_HORIZON_DAYS)
    items: list[ActionItem] = []
    qs = Lease.objects.filter(
        landlord=landlord,
        status=Lease.LeaseStatus.ACTIVE,
        is_month_to_month=False,
        end_date__isnull=False,
        end_date__gte=today,
        end_date__lte=horizon,
    ).select_related("property")
    for lease in qs:
        items.append(
            ActionItem(
                key=f"lease.expiring:{lease.pk}",
                severity="info",
                title=f"Fixed term ends {lease.end_date.strftime('%b %-d')}",
                detail=f"{_place(lease)} — renew, go month-to-month, or plan the move-out inspection",
                url=f"/dashboard/leases/{lease.pk}",
                due_date=lease.end_date,
                source="lease",
            )
        )
    return items


def _overdue_charges(landlord) -> list[ActionItem]:
    """
    One item per lease with overdue money — same queryset math as the
    summary endpoint. `outstanding` is itself an annotation, so the
    per-lease fold happens in Python (a landlord's open charges are dozens,
    not thousands).
    """
    from collections import defaultdict

    from rentium.ledger.models import INCOME_CHARGE_TYPES, LedgerEntry

    today = date.today()
    charges = (
        LedgerEntry.objects.with_settlement()
        .filter(
            landlord=landlord,
            entry_type__in=INCOME_CHARGE_TYPES,
            reversed_by__isnull=True,
            due_date__lt=today,
            outstanding__gt=0,
        )
        .select_related("property")
    )
    by_lease: dict = defaultdict(lambda: {"n": 0, "total": 0, "place": ""})
    for charge in charges:
        row = by_lease[charge.lease_id]
        row["n"] += 1
        row["total"] += charge.outstanding
        row["place"] = charge.property.name if charge.property else "Unassigned"

    items: list[ActionItem] = []
    for lease_id, row in by_lease.items():
        items.append(
            ActionItem(
                key=f"ledger.overdue.lease:{lease_id}",
                severity="urgent",
                title=f"{row['n']} overdue charge{'s' if row['n'] != 1 else ''} — ${row['total']}",
                detail=f"{row['place']} — outstanding and past due",
                url="/dashboard/financial",
                due_date=None,
                source="ledger",
            )
        )
    return items


def _stale_work_orders(landlord) -> list[ActionItem]:
    from django.utils import timezone

    from rentium.maintenance.models import WorkOrder

    now = timezone.now()
    items: list[ActionItem] = []
    qs = WorkOrder.objects.filter(
        property__landlord=landlord,
        status=WorkOrder.Status.NEW,
    ).select_related("property")
    for wo in qs:
        breached = bool(wo.sla_due_at and wo.sla_due_at < now)
        items.append(
            ActionItem(
                key=f"maintenance.new:{wo.pk}",
                severity="urgent" if breached else "soon",
                title=("SLA breached: " if breached else "New request: ") + wo.title,
                detail=wo.property.name,
                url="/dashboard/maintenance",
                due_date=wo.sla_due_at.date() if wo.sla_due_at else None,
                source="maintenance",
            )
        )
    return items


SOURCES = (
    _missing_move_in_inspections,
    _inspection_delivery_due,
    _stalled_signatures,
    _expiring_leases,
    _overdue_charges,
    _stale_work_orders,
)


def compute_attention(landlord) -> list[ActionItem]:
    """Everything that needs doing, most urgent first, then by due date."""
    items: list[ActionItem] = []
    for source in SOURCES:
        items.extend(source(landlord))
    items.sort(
        key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.due_date or date.max)
    )
    return items
