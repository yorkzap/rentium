"""
Sergeants — deterministic, $0-LLM watchers. Every function here is pure
Python over the ledger/Constitution/leases, run on Celery beat, IDEMPOTENT
and dedup-safe against the DomainEvent outbox (same pattern as
ledger/daily.py — re-running any function, any number of times, never
double-fires a finding).

A finding is a `rama.sentinel.<kind>` DomainEvent with `landlord_id` in its
payload (these are portfolio/holding-scoped, not always property/lease-
scoped, so they can't always rely on notify.py's property/lease-based
landlord resolution — see events/notify.py `_landlord_user`'s payload
fallback). rama/handlers.py turns each into a bounded FSA analysis via
Celery; the FSA only ever REASONS over the facts computed here — it never
invents thresholds or arithmetic.
"""

from __future__ import annotations

import logging
import statistics
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

STALE_AFTER_DAYS = 14  # mirrors rama.finance.STALE_AFTER_DAYS
DEPOSIT_RETURN_DAYS = 15  # BC RTB: 15 days from later of tenancy end / forwarding addr
DEPOSIT_WARN_DAYS = 5
LATE_THRESHOLD_DAYS = 2  # a payment counts "late" past this many days
LATE_MIN_OCCURRENCES = 3  # in the trailing window before it's a "pattern"
LATE_WINDOW_DAYS = 180
EXPENSE_ANOMALY_RATIO = 1.5  # this month vs trailing mean
EXPENSE_ANOMALY_MIN_DELTA = Decimal("50.00")  # ignore tiny categories
SURPLUS_BUFFER_PCT = Decimal("0.10")
SURPLUS_MIN_TO_FLAG = Decimal("500.00")


def _already_published(event_type: str, dedupe_key: str) -> bool:
    from rentium.events.models import DomainEvent

    return DomainEvent.objects.filter(
        event_type=event_type, payload__dedupe_key=dedupe_key
    ).exists()


def _publish_finding(event_type: str, dedupe_key: str, landlord, payload: dict, **kw):
    from rentium.events.registry import publish

    if _already_published(event_type, dedupe_key):
        return False
    try:
        publish(
            event_type,
            {"dedupe_key": dedupe_key, "landlord_id": str(landlord.pk), **payload},
            **kw,
        )
        return True
    except Exception:  # noqa: BLE001 — one bad finding must not sink the run
        logger.exception("%s publish failed (%s)", event_type, dedupe_key)
        return False


# --------------------------------------------------------------- balances
def check_min_balances() -> dict:
    """Landlord-reported balance vs each active MIN_BALANCE Constitution
    rule. Fires 'breach' (below the rule) or 'stale' (nothing recent
    reported) — never both, and never when healthy."""
    from rentium.ledger.models import PropertyBankBalance
    from rentium.rama.models import RamaConstitutionRule

    checked, breaches, staleness = 0, 0, 0
    rules = RamaConstitutionRule.objects.filter(
        rule_type=RamaConstitutionRule.RuleType.MIN_BALANCE, active=True
    ).select_related("landlord")
    for rule in rules.iterator():
        params = rule.params or {}
        holding_id = params.get("holding_id") or params.get("property_id")
        try:
            min_amount = Decimal(str(params.get("amount", "0")))
        except (InvalidOperation, TypeError):
            continue
        checked += 1
        row = PropertyBankBalance.objects.filter(
            landlord=rule.landlord, holding_id=holding_id
        ).first()
        if row is None:
            continue  # nothing reported yet — nothing to compare
        stale = (date.today() - row.as_of).days > STALE_AFTER_DAYS
        if stale:
            stage = "stale"
        elif row.balance < min_amount:
            stage = "breach"
        else:
            continue  # healthy
        key = f"minbal:{rule.pk}:{row.as_of}:{stage}"
        if _publish_finding(
            "rama.sentinel.min_balance", key, rule.landlord,
            {
                "rule_id": rule.pk,
                "holding_id": str(holding_id) if holding_id else None,
                "holding_name": row.holding.name if row.holding_id else "portfolio",
                "balance": str(row.balance),
                "min_amount": str(min_amount),
                "as_of": str(row.as_of),
                "stage": stage,
                "severity": "URGENT" if stage == "breach" else "INFO",
            },
        ):
            breaches += stage == "breach"
            staleness += stage == "stale"
    return {"rules_checked": checked, "breaches": breaches, "stale_flags": staleness}


# ------------------------------------------------------- deposit deadlines
def check_deposit_return_deadlines() -> dict:
    """BC RTB: return the deposit within 15 days of the LATER of tenancy end
    and forwarding-address receipt. We don't store a separate "forwarding
    address received" timestamp yet, so the clock starts once an address is
    on file, anchored to tenancy end (a documented simplification — the
    address usually arrives at or after move-out; this is the earliest the
    clock could legitimately start, so it never UNDER-warns)."""
    from django.db.models import Sum

    from rentium.leases.inspections import ConditionInspection
    from rentium.leases.models import Lease
    from rentium.ledger.models import EntryType, LedgerEntry

    today = date.today()
    published = 0
    final_statuses = [Lease.LeaseStatus.TERMINATED, Lease.LeaseStatus.EXPIRED]

    leases = (
        Lease.objects.filter(status__in=final_statuses)
        .select_related("landlord", "property")
        .iterator()
    )
    for lease in leases:
        held = (
            LedgerEntry.objects.not_voided()
            .filter(
                landlord=lease.landlord, lease=lease, entry_type=EntryType.PAYMENT,
                settles__entry_type=EntryType.DEPOSIT_CHARGE,
            )
            .aggregate(s=Sum("amount"))["s"]
        )
        returned = (
            LedgerEntry.objects.not_voided()
            .filter(landlord=lease.landlord, lease=lease, entry_type=EntryType.DEPOSIT_RETURN)
            .aggregate(s=Sum("amount"))["s"]
        )
        outstanding_deposit = (held or Decimal("0")) - (returned or Decimal("0"))
        if outstanding_deposit <= 0:
            continue

        insp = (
            ConditionInspection.objects.filter(lease=lease)
            .exclude(tenant_forwarding_address="")
            .order_by("-created_at")
            .first()
        )
        if insp is None:
            continue  # no forwarding address on file — clock hasn't started

        tenancy_end = lease.move_out_date or lease.end_date
        if not tenancy_end:
            continue
        deadline = tenancy_end + timedelta(days=DEPOSIT_RETURN_DAYS)
        days_left = (deadline - today).days
        if days_left > DEPOSIT_WARN_DAYS:
            continue
        stage = "overdue" if days_left < 0 else "due_soon"
        key = f"depdl:{lease.pk}:{stage}"
        if _publish_finding(
            "rama.sentinel.deposit_deadline", key, lease.landlord,
            {
                "lease_id": str(lease.pk),
                "lease_number": lease.lease_number,
                "property": lease.property.name if lease.property_id else "",
                "deadline": str(deadline),
                "outstanding_deposit": str(outstanding_deposit),
                "stage": stage,
                "severity": "URGENT" if stage == "overdue" else "WARN",
            },
            property_id=lease.property_id, lease_id=lease.pk,
        ):
            published += 1
    return {"findings_published": published}


# --------------------------------------------------------- late patterns
def profile_late_patterns() -> dict:
    """Tenants with a repeat pattern of late rent payments in the trailing
    window. Facts only (counts, median days late, whether a late fee was
    ever charged) — the FSA/General decide what (if anything) to do."""
    from rentium.leases.models import Lease, LeaseTenant
    from rentium.ledger.models import EntryType, LedgerEntry

    since = date.today() - timedelta(days=LATE_WINDOW_DAYS)
    published = 0

    payments = (
        LedgerEntry.objects.not_voided()
        .filter(
            entry_type=EntryType.PAYMENT,
            settles__entry_type=EntryType.RENT_CHARGE,
            effective_date__gte=since,
            settles__due_date__isnull=False,
        )
        .select_related("settles", "lease", "landlord")
    )
    by_lease: dict = {}
    for p in payments.iterator():
        days_late = (p.effective_date - p.settles.due_date).days
        by_lease.setdefault(p.lease_id, {"landlord": p.landlord, "lease": p.lease, "late": []})
        if days_late > LATE_THRESHOLD_DAYS:
            by_lease[p.lease_id]["late"].append(days_late)

    for lease_id, bucket in by_lease.items():
        late = bucket["late"]
        if len(late) < LATE_MIN_OCCURRENCES:
            continue
        lease = bucket["lease"]
        fee_ever = LedgerEntry.objects.not_voided().filter(
            lease_id=lease_id, entry_type=EntryType.FEE_CHARGE
        ).exists()
        primary = (
            LeaseTenant.objects.filter(lease_id=lease_id, is_primary_tenant=True)
            .select_related("tenant__user")
            .first()
        )
        tenant_name = primary.display_name if primary else ""
        key = f"late:{lease_id}:{date.today().strftime('%Y-%m')}"
        if _publish_finding(
            "rama.sentinel.late_pattern", key, bucket["landlord"],
            {
                "lease_id": str(lease_id),
                "lease_number": lease.lease_number if lease else "",
                "property": lease.property.name if lease and lease.property_id else "",
                "tenant_name": tenant_name,
                "late_count": len(late),
                "median_days_late": statistics.median(late),
                "late_fee_ever_charged": fee_ever,
                "severity": "WARN",
            },
            property_id=lease.property_id if lease else None, lease_id=lease_id,
        ):
            published += 1
    return {"findings_published": published}


# ------------------------------------------------------- expense anomalies
def detect_expense_anomalies() -> dict:
    """This month's expenses vs each property/category's trailing average.
    Flags only meaningful jumps (ratio AND absolute-dollar gate) — a $20
    category doubling isn't worth an alert."""
    from django.db.models import Sum

    from rentium.ledger.models import EntryType, LedgerEntry
    from rentium.properties.models import Property

    today = date.today()
    month_start = today.replace(day=1)
    trailing_start = (month_start - timedelta(days=95)).replace(day=1)
    published = 0

    rows = (
        LedgerEntry.objects.not_voided()
        .filter(
            entry_type=EntryType.EXPENSE, property__isnull=False,
            effective_date__gte=trailing_start,
        )
        .values("property_id", "category", "effective_date__year", "effective_date__month")
        .annotate(total=Sum("amount"))
    )
    # {(property_id, category): {"YYYY-MM": total}}
    grid: dict = {}
    for r in rows:
        k = (r["property_id"], r["category"])
        month_key = f"{r['effective_date__year']:04d}-{r['effective_date__month']:02d}"
        grid.setdefault(k, {})[month_key] = r["total"]

    this_month_key = month_start.strftime("%Y-%m")
    for (property_id, category), months in grid.items():
        this_month = months.get(this_month_key)
        prior = [v for k, v in months.items() if k != this_month_key]
        if not this_month or len(prior) < 2:
            continue
        trailing_mean = statistics.mean(prior)
        if trailing_mean <= 0:
            continue
        delta = this_month - trailing_mean
        if this_month < trailing_mean * Decimal(str(EXPENSE_ANOMALY_RATIO)):
            continue
        if delta < EXPENSE_ANOMALY_MIN_DELTA:
            continue
        prop = Property.objects.filter(pk=property_id).select_related("landlord").first()
        if prop is None:
            continue
        key = f"expanom:{property_id}:{category}:{this_month_key}"
        if _publish_finding(
            "rama.sentinel.expense_anomaly", key, prop.landlord,
            {
                "property": prop.name, "category": category,
                "this_month": str(this_month), "trailing_mean": str(round(trailing_mean, 2)),
                "month": this_month_key, "severity": "WARN",
            },
            property_id=prop.pk,
        ):
            published += 1
    return {"findings_published": published}


# ---------------------------------------------------------------- surplus
def compute_surplus() -> dict:
    """Reported balance − upcoming committed expenses (30 days) − a safety
    buffer. Flags real, meaningful surplus only (floor, not noise)."""
    from django.db.models import Q, Sum

    from rentium.ledger.models import EntryType, LedgerEntry, PropertyBankBalance

    published = 0
    horizon = date.today() + timedelta(days=30)
    for row in PropertyBankBalance.objects.select_related("holding", "landlord").iterator():
        props_filter = {"holding_id": row.holding_id} if row.holding_id else {}
        from rentium.properties.models import Property

        prop_ids = Property.objects.filter(landlord=row.landlord, **props_filter).values_list(
            "pk", flat=True
        )
        scope = (
            Q(property_id__in=list(prop_ids)) | Q(holding_id=row.holding_id)
            if row.holding_id
            else Q(landlord=row.landlord)
        )
        committed = (
            LedgerEntry.objects.not_voided()
            .filter(
                scope,
                landlord=row.landlord,
                entry_type=EntryType.EXPENSE, paid_on__isnull=True,
                effective_date__lte=horizon,
            )
            .aggregate(s=Sum("amount"))["s"]
            or Decimal("0")
        )
        buffer = row.balance * SURPLUS_BUFFER_PCT
        surplus = row.balance - committed - buffer
        if surplus < SURPLUS_MIN_TO_FLAG:
            continue
        key = f"surplus:{row.pk}:{row.as_of}"
        if _publish_finding(
            "rama.sentinel.surplus", key, row.landlord,
            {
                "holding_name": row.holding.name if row.holding_id else "portfolio",
                "holding_id": str(row.holding_id) if row.holding_id else None,
                "balance": str(row.balance), "committed_30d": str(committed),
                "buffer": str(round(buffer, 2)), "surplus": str(round(surplus, 2)),
                "as_of": str(row.as_of), "severity": "INFO",
            },
        ):
            published += 1
    return {"findings_published": published}


def run_all() -> dict:
    """Run every Sergeant in order and return a combined report — the
    single entry point the beat task and management command call."""
    report = {}
    for name, fn in (
        ("min_balances", check_min_balances),
        ("deposit_deadlines", check_deposit_return_deadlines),
        ("late_patterns", profile_late_patterns),
        ("expense_anomalies", detect_expense_anomalies),
        ("surplus", compute_surplus),
        ("mortgage_renewals", check_mortgage_renewals),
        ("valuation_staleness", check_valuation_staleness),
        ("spend_drift", check_spend_drift),
    ):
        try:
            report[name] = fn()
        except Exception:  # noqa: BLE001 — one Sergeant failing must not sink the rest
            logger.exception("Sergeant %s failed", name)
            report[name] = {"error": True}
    return report


# ---------------------------------------------------------------------------
# Finance watchers. Same shape as the five above: deterministic, $0, idempotent
# via dedupe_key. These are what make the Treasurer notice things rather than
# only answering when asked.
# ---------------------------------------------------------------------------
RENEWAL_WARN_DAYS = 120
VALUATION_STALE_DAYS = 730
SPEND_DRIFT_RATIO = Decimal("1.30")
SPEND_DRIFT_MIN_DELTA = Decimal("300.00")


def check_mortgage_renewals() -> dict:
    """A renewal is the one dated financial decision a landlord can miss.

    Warned early on purpose: a rate hold has to be arranged before the term
    ends, so finding out on the day is finding out too late.
    """
    from datetime import date, timedelta

    from rentium.ledger.models import HoldingMortgage

    found = 0
    horizon = date.today() + timedelta(days=RENEWAL_WARN_DAYS)
    for mortgage in HoldingMortgage.objects.filter(
        status=HoldingMortgage.Status.ACTIVE,
        term_end__isnull=False,
        term_end__lte=horizon,
        term_end__gte=date.today(),
    ).select_related("holding", "landlord"):
        days = (mortgage.term_end - date.today()).days
        if _publish_finding(
            "rama.sentinel.mortgage_renewal",
            f"renewal:{mortgage.pk}:{mortgage.term_end.isoformat()}",
            mortgage.landlord,
            {
                "holding": mortgage.holding.name,
                "days_to_renewal": days,
                "term_end": mortgage.term_end.isoformat(),
                "rate_percent": str(mortgage.rate_percent or ""),
                "lender": mortgage.lender,
                "severity": "WARN",
            },
        ):
            found += 1
    return {"findings_published": found}


def check_valuation_staleness() -> dict:
    """Equity computed off a years-old valuation is a confident wrong number."""
    from datetime import date, timedelta

    from rentium.properties.models import PropertyHolding

    found = 0
    cutoff = date.today() - timedelta(days=VALUATION_STALE_DAYS)
    for holding in PropertyHolding.objects.select_related("landlord"):
        latest = holding.valuations.order_by("-as_of").first()
        if latest is None or latest.as_of > cutoff:
            continue
        if _publish_finding(
            "rama.sentinel.valuation_stale",
            f"valstale:{holding.pk}:{latest.as_of.isoformat()}",
            holding.landlord,
            {
                "holding": holding.name,
                "last_valued": latest.as_of.isoformat(),
                "amount": str(latest.amount),
                "basis": latest.basis,
                "severity": "INFO",
            },
        ):
            found += 1
    return {"findings_published": found}


def check_spend_drift() -> dict:
    """A category quietly costing more than it used to.

    Distinct from detect_expense_anomalies, which looks at one month against a
    trailing mean. This compares a full year against the year before, so a
    slow creep shows up where a monthly spike would not.
    """
    from datetime import date, timedelta

    from django.db.models import Sum

    from rentium.ledger.models import EntryType, LedgerEntry
    from rentium.users.models import LandlordProfile

    today = date.today()
    this_year = (today - timedelta(days=365), today)
    last_year = (today - timedelta(days=730), today - timedelta(days=365))
    found = 0

    for landlord in LandlordProfile.objects.all():
        def totals(window):
            rows = (
                LedgerEntry.objects.not_voided()
                .filter(
                    landlord=landlord,
                    entry_type=EntryType.EXPENSE,
                    effective_date__gte=window[0],
                    effective_date__lt=window[1],
                )
                .values("category")
                .annotate(total=Sum("amount"))
            )
            return {r["category"]: (r["total"] or Decimal("0")) for r in rows}

        now, before = totals(this_year), totals(last_year)
        for category, amount in now.items():
            prior = before.get(category)
            if not prior or prior <= 0:
                continue
            delta = amount - prior
            if amount / prior < SPEND_DRIFT_RATIO or delta < SPEND_DRIFT_MIN_DELTA:
                continue
            if _publish_finding(
                "rama.sentinel.spend_drift",
                f"drift:{landlord.pk}:{category}:{today.strftime('%Y-%m')}",
                landlord,
                {
                    "category": category,
                    "this_year": str(amount),
                    "last_year": str(prior),
                    "increase": str(delta),
                    "severity": "WARN",
                },
            ):
                found += 1
    return {"findings_published": found}
