# rentium/ledger/daily.py
"""
Daily housekeeping for Rentium — the jobs that keep time-based state true.

WHY THIS MODULE EXISTS: without it, the app silently rots. Rent charges are
only generated 45 days ahead (GENERATION_HORIZON_DAYS) and only when a
signal fires (a signature, an adjustment, a roommate change). On a quiet
lease nothing re-triggers generation, so around month 2-3 new rent charges
just stop appearing and "Expected This Month" drops to $0. Similarly,
leases never flip to EXPIRED on their own, tenants never get "rent due
soon" nudges, and maintenance SLA breaches are never detected.

Each function below is IDEMPOTENT and safe to run any number of times per
day:
  - rent charges dedupe on their natural idempotency keys;
  - lease expiry only touches ACTIVE leases whose end_date has passed;
  - reminders and SLA breaches dedupe against the DomainEvent outbox, so a
    re-run (or a twice-daily cron) never double-notifies.

Run it two ways (both call the same functions):
  1. Celery beat  -> rentium/ledger/tasks.py (preferred once Celery is up)
  2. cron / manual -> python manage.py run_daily_tasks
"""

import logging
from datetime import date
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# How many days before a charge's due date the "due soon" reminder fires.
# 0 is included so tenants also get a nudge on the due date itself.
DUE_SOON_OFFSETS = (3, 0)


# ------------------------------------------------------------ rent charges
def generate_all_rent_charges() -> dict:
    """
    Roll the 45-day generation horizon forward for every ACTIVE lease.
    Per-lease try/except so one bad lease can't starve the rest of the
    portfolio of rent charges.
    """
    from rentium.leases.models import Lease
    from rentium.ledger.billing import generate_rent_charges_for_lease

    created_total, leases_touched, failures = 0, 0, 0
    for lease in Lease.objects.filter(status=Lease.LeaseStatus.ACTIVE).iterator():
        try:
            created = generate_rent_charges_for_lease(lease)
            created_total += created
            leases_touched += 1
        except Exception:
            failures += 1
            logger.exception("Rent generation failed for lease %s", lease.pk)
    return {
        "leases_processed": leases_touched,
        "charges_created": created_total,
        "failures": failures,
    }


# ------------------------------------------------------------ lease expiry
def expire_ended_leases() -> dict:
    """
    ACTIVE fixed-term leases whose end_date has fully passed become EXPIRED.
    (A lease ends ON its end_date, so we only expire when today is strictly
    after it.) Month-to-month leases have no end_date and are never touched.
    The existing close_occupancies_on_end signal fires off this save and
    closes the occupancy log rows; we also publish lease.expired so both
    parties get a notification (ROUTES already maps it to BOTH).
    """
    from rentium.events.registry import publish
    from rentium.leases.models import Lease

    today = date.today()
    expired, failures = 0, 0
    qs = Lease.objects.filter(
        status=Lease.LeaseStatus.ACTIVE,
        end_date__isnull=False,
        end_date__lt=today,
    )
    for lease in qs.iterator():
        try:
            lease.status = Lease.LeaseStatus.EXPIRED
            lease.save(update_fields=["status", "updated_at"])
            publish(
                "lease.expired",
                {"lease_id": str(lease.pk), "end_date": lease.end_date.isoformat()},
                property_id=lease.property_id,
                lease_id=lease.pk,
            )
            expired += 1
        except Exception:
            failures += 1
            logger.exception("Failed to expire lease %s", lease.pk)
    return {"leases_expired": expired, "failures": failures}


# ------------------------------------------------------- due-soon reminders
def publish_charge_due_reminders(offsets=DUE_SOON_OFFSETS) -> dict:
    """
    For each offset N in `offsets`, find income charges due exactly N days from
    today that still have an outstanding balance, and publish a
    ledger.charge_due_soon event for each.

    BUG FIXED: this used to filter `tenant__isnull=False`. Joint (roommate)
    household charges are, by definition, the ones with tenant=NULL — the whole
    point of billing.py's JOINT mode. So EVERY roommate lease in the system was
    silently receiving zero rent reminders, forever, while split-billing leases
    got them fine. Nobody would have noticed until a tenant asked why they
    never got a heads-up.

    The correct filter is "the charge is owed by SOMEONE on a lease": a named
    tenant OR a whole household. Recipient resolution then happens in
    events/notify.py, whose TENANT audience already fans a lease-scoped event
    out to every linked tenant — which is exactly right for a joint charge, and
    consistent with the joint-and-several policy (everyone is on the hook for
    the full rent, so everyone hears that money is due).

    Dedup: each (charge, stage) pair is published at most once EVER, enforced
    against the DomainEvent outbox, so re-running the task the same day (or a
    Celery retry) can't double-notify.
    """
    from rentium.events.models import DomainEvent
    from rentium.events.registry import publish
    from rentium.ledger.models import INCOME_CHARGE_TYPES
    from rentium.ledger.models import LedgerEntry

    today = date.today()
    published, skipped = 0, 0

    for offset in offsets:
        target = today + timedelta(days=offset)
        stage = f"due_in_{offset}"

        charges = (
            LedgerEntry.objects.with_settlement()
            .filter(
                entry_type__in=INCOME_CHARGE_TYPES,
                reversed_by__isnull=True,
                due_date=target,
                outstanding__gt=0,
                lease__isnull=False,  # a lease is what tells us WHO to tell.
            )
            .select_related("lease")
        )

        for charge in charges.iterator():
            already = DomainEvent.objects.filter(
                event_type="ledger.charge_due_soon",
                payload__charge_id=str(charge.pk),
                payload__stage=stage,
            ).exists()
            if already:
                skipped += 1
                continue

            try:
                publish(
                    "ledger.charge_due_soon",
                    {
                        "charge_id": str(charge.pk),
                        "stage": stage,
                        "amount_outstanding": str(charge.outstanding),
                        "due_date": charge.due_date.isoformat(),
                        "description": charge.description,
                        # Lets the notification copy say "your household owes"
                        # rather than "you owe" on a shared charge.
                        "joint": charge.tenant_id is None,
                    },
                    property_id=charge.property_id,
                    lease_id=charge.lease_id,
                )
                published += 1
            except Exception:
                logger.exception(
                    "charge_due_soon publish failed for charge %s", charge.pk
                )

    return {"reminders_published": published, "already_sent": skipped}


# ---------------------------------------------------------- SLA breaches
def flag_sla_breaches() -> dict:
    """
    Any open work order past its sla_due_at gets a maintenance.sla_breached
    event (routed to the LANDLORD via the property fallback in notify.py).
    Dedup against the DomainEvent outbox: one breach event per work order,
    ever — the breach doesn't un-happen, so there's nothing to re-announce.

    If your WorkOrder model stores a concrete `sla_breached` boolean, it's
    flipped too (best-effort; skipped harmlessly if it's a computed
    property instead).
    """
    from rentium.events.models import DomainEvent
    from rentium.events.registry import publish
    from rentium.maintenance.models import WorkOrder

    now = timezone.now()
    terminal = [WorkOrder.Status.COMPLETED, WorkOrder.Status.CANCELLED]
    overdue = WorkOrder.objects.filter(
        sla_due_at__isnull=False, sla_due_at__lt=now
    ).exclude(status__in=terminal)

    published, skipped = 0, 0
    for wo in overdue.iterator():
        already = DomainEvent.objects.filter(
            event_type="maintenance.sla_breached",
            payload__work_order_id=str(wo.pk),
        ).exists()
        if already:
            skipped += 1
            continue
        try:
            publish(
                "maintenance.sla_breached",
                {
                    "work_order_id": str(wo.pk),
                    "title": wo.title,
                    "sla_due_at": wo.sla_due_at.isoformat(),
                    "rta_emergency": bool(getattr(wo, "is_rta_emergency", False)),
                },
                property_id=wo.property_id,
            )
            published += 1
            try:
                type(wo).objects.filter(pk=wo.pk).update(sla_breached=True)
            except Exception:
                pass
        except Exception:
            logger.exception("sla_breached publish failed for work order %s", wo.pk)
    return {"breaches_published": published, "already_flagged": skipped}


# ------------------------------------------- inspection delivery deadlines
INSPECTION_DELIVERY_WARN_DAYS = 2


def publish_inspection_delivery_reminders() -> dict:
    """
    BC compliance clocks: a signed move-in report must reach the tenant
    within 7 days of the inspection date; a signed move-out report within
    15 days. Missing these deadlines extinguishes the landlord's deposit
    claims — so warn at T-2 days and once more when overdue. Dedup per
    (inspection, pass, stage) via the DomainEvent outbox, so re-runs never
    double-notify. mark_delivered on the inspection stops the clock.
    """
    from datetime import timedelta as _td

    from rentium.events.models import DomainEvent
    from rentium.events.registry import publish
    from rentium.leases.inspections import ConditionInspection

    today = date.today()
    published = 0

    def _check(insp, pass_name, signed_date, window_days, delivered_at):
        nonlocal published
        if not signed_date or delivered_at:
            return
        deadline = signed_date + _td(days=window_days)
        days_left = (deadline - today).days
        if days_left > INSPECTION_DELIVERY_WARN_DAYS:
            return
        stage = "overdue" if days_left < 0 else "due_soon"
        already = DomainEvent.objects.filter(
            event_type="inspection.delivery_due",
            payload__inspection_id=str(insp.pk),
            payload__pass=pass_name,
            payload__stage=stage,
        ).exists()
        if already:
            return
        try:
            publish(
                "inspection.delivery_due",
                {
                    "inspection_id": str(insp.pk),
                    "pass": pass_name,
                    "stage": stage,
                    "deadline": deadline.isoformat(),
                    "lease_number": insp.lease.lease_number,
                },
                property_id=insp.lease.property_id,
                lease_id=insp.lease_id,
            )
            published += 1
        except Exception:
            logger.exception(
                "Delivery reminder publish failed for inspection %s", insp.pk
            )

    qs = ConditionInspection.objects.select_related("lease").exclude(
        status=ConditionInspection.Status.MOVE_IN_IN_PROGRESS
    )
    for insp in qs.iterator():
        _check(
            insp,
            "MOVE_IN",
            insp.move_in_inspection_date,
            7,
            insp.move_in_report_delivered_at,
        )
        if insp.status == ConditionInspection.Status.COMPLETED:
            _check(
                insp,
                "MOVE_OUT",
                insp.move_out_inspection_date,
                15,
                insp.move_out_report_delivered_at,
            )
    return {"delivery_reminders_published": published}


# ------------------------------------------------------------------ runner
def run_all() -> dict:
    """Run every daily job in dependency-sensible order and return a report.

    Order matters slightly: expire leases FIRST so generate_all_rent_charges
    doesn't roll the horizon forward for a lease that ended yesterday
    (generation is gated on ACTIVE, and _rent_due_dates respects end_date
    anyway — this ordering just makes the invariant obvious)."""
    report = {}
    report["expire_ended_leases"] = expire_ended_leases()
    report["generate_all_rent_charges"] = generate_all_rent_charges()
    report["publish_charge_due_reminders"] = publish_charge_due_reminders()
    report["publish_inspection_delivery_reminders"] = (
        publish_inspection_delivery_reminders()
    )
    report["flag_sla_breaches"] = flag_sla_breaches()
    logger.info("Daily housekeeping report: %s", report)
    return report
