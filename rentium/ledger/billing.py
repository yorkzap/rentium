# billing.py

"""
Billing engine on top of the ledger.

- Rent is charged on the 1st of the month, except the first billing period,
  which is charged on the tenant's effective move-in date and prorated via
  the auto-created PRORATION RentAdjustment.
- Generation is idempotent by natural key. The daily task and the
  activation hook can both run without double-charging.
- A RentAdjustment created AFTER charges exist is applied by
  apply_adjustment_to_ledger():
    * untouched future/unpaid charge  -> void + repost at the new amount
      (the schedule shows the right number, the audit trail keeps the old one)
    * charge with money already on it -> post a CREDIT for the difference
      (received money is historical fact and is never rewritten)
- Utility bills are split by *temporal occupancy* (who actually lived there
  during the billing period), not by today's room assignments — so
  retroactive bills stay correct after roommate swaps.

JOINT vs SPLIT billing
======================
Roommate agreements are joint-and-several: the WHOLE household owes the
WHOLE rent and the WHOLE deposit, and either tenant may pay any of it.
For those leases we post ONE household charge per period with tenant=None
(natural key rent:<lease>:joint:<due_date>). Every tenant on the lease
sees it (viewset scoping), any tenant's payment settles it (payments carry
tenant=<payer> so who-paid-what is preserved), and it clears for everyone
at once. This also fixes the "unlinked second tenant was never charged"
hole: the joint amount is the sum over ALL non-declined LeaseTenant rows,
linked or not.

SPLIT mode (each tenant billed their own share) is kept for leases that
genuinely bill individually and remains the old per-tenant behaviour.

Mode selection: an optional Lease.billing_mode field ("JOINT"/"SPLIT")
wins if present; otherwise any lease whose lease_type contains "ROOMMATE"
is JOINT and everything else is SPLIT.
"""

from datetime import date
from datetime import timedelta
from decimal import ROUND_HALF_UP
from decimal import Decimal

from django.db.models import Q

from rentium.leases.models import Lease
from rentium.leases.models import LeaseTenant

from .models import EntryType
from .models import LedgerEntry
from .services import post_charge
from .services import post_credit
from .services import post_expense
from .services import void_entry

GENERATION_HORIZON_DAYS = 45


# ---------------------------------------------------------------- helpers
def _first_of_next_month(d: date) -> date:
    return date(d.year + (d.month // 12), (d.month % 12) + 1, 1)


def _effective_start(lt: LeaseTenant) -> date:
    default = lt.individual_start_date or lt.lease.move_in_date or lt.lease.start_date
    fixed_target = (
        lt.rent_adjustments.filter(
            is_recurring=False,
            target_amount__isnull=False,
            effective_date__gte=lt.lease.start_date,
            effective_date__lt=default,
        )
        .order_by("effective_date")
        .values_list("effective_date", flat=True)
        .first()
    )
    return fixed_target or default


def _effective_end(lt: LeaseTenant):
    return lt.individual_end_date or lt.lease.end_date  # None = month-to-month


def lease_is_joint(lease: Lease) -> bool:
    """
    JOINT = one household bill, either tenant can pay (roommate agreements).
    SPLIT = each tenant billed their own share.
    An explicit Lease.billing_mode field wins; otherwise infer from type.
    """
    mode = (getattr(lease, "billing_mode", "") or "").upper()
    if mode == "JOINT":
        return True
    if mode == "SPLIT":
        return False
    return "ROOMMATE" in ((getattr(lease, "lease_type", "") or "").upper())


def rent_key(lt: LeaseTenant, due: date) -> str:
    return f"rent:{lt.lease_id}:{lt.tenant_id}:{due.isoformat()}"


def joint_rent_key(lease: Lease, due: date) -> str:
    return f"rent:{lease.pk}:joint:{due.isoformat()}"


def compute_rent_for_due_date(lt: LeaseTenant, due_date: date) -> Decimal:
    """Base rent net of every RentAdjustment active on `due_date`."""
    amount = Decimal(lt.rent_amount)
    adjustments = (
        lt.rent_adjustments.filter(effective_date__lte=due_date)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=due_date))
        .order_by("effective_date", "created_at")
    )
    for adj in adjustments:
        amount = adj.get_adjusted_amount(amount)
    return amount.quantize(Decimal("0.01"))


def compute_joint_rent_for_due_date(lease: Lease, due_date: date) -> Decimal:
    """
    Household rent for the period: the sum over ALL non-declined
    LeaseTenant rows — including pending invites (tenant=None), because
    joint-and-several means the household owes the full rent whether or
    not every roommate has linked an account yet.
    Per-tenant RentAdjustments still apply (a discount on one roommate's
    share lowers the household total).
    """
    total = Decimal("0.00")
    for lt in lease.lease_tenants.filter(declined=False):
        total += compute_rent_for_due_date(lt, due_date)
    return total.quantize(Decimal("0.01"))


def _rent_due_dates(lt: LeaseTenant, until: date):
    start, end = _effective_start(lt), _effective_end(lt)
    due = start
    while due <= until:
        if end and due > end:
            return
        yield due
        due = _first_of_next_month(due)


def _lease_rent_due_dates(lease: Lease, until: date):
    """Lease-level schedule, including explicit pre-possession targets."""
    start = lease.move_in_date or lease.start_date
    fixed_target = (
        lease.lease_tenants.filter(
            declined=False,
            rent_adjustments__is_recurring=False,
            rent_adjustments__target_amount__isnull=False,
            rent_adjustments__effective_date__gte=lease.start_date,
            rent_adjustments__effective_date__lt=start,
        )
        .order_by("rent_adjustments__effective_date")
        .values_list("rent_adjustments__effective_date", flat=True)
        .first()
    )
    start = fixed_target or start
    end = lease.end_date
    due = start
    while due <= until:
        if end and due > end:
            return
        yield due
        due = _first_of_next_month(due)


# ------------------------------------------------------------ generation
def ensure_rent_charge(lt: LeaseTenant, due_date: date):
    """SPLIT mode: one tenant's own rent charge for the period."""
    if lt.tenant_id is None:  # pending invite — no one to charge yet
        return None, False
    amount = compute_rent_for_due_date(lt, due_date)
    if amount <= 0:
        return None, False
    return post_charge(
        landlord=lt.lease.landlord,
        tenant=lt.tenant,
        lease=lt.lease,
        amount=amount,
        due_date=due_date,
        entry_type=EntryType.RENT_CHARGE,
        description=f"Monthly rent — period starting {due_date}",
        idempotency_key=rent_key(lt, due_date),
        metadata={"period_start": due_date.isoformat()},
    )


def ensure_joint_rent_charge(lease: Lease, due_date: date):
    """JOINT mode: one household rent charge (tenant=None) for the period."""
    amount = compute_joint_rent_for_due_date(lease, due_date)
    if amount <= 0:
        return None, False
    return post_charge(
        landlord=lease.landlord,
        tenant=None,  # household charge — every tenant on the lease sees it
        lease=lease,
        amount=amount,
        due_date=due_date,
        entry_type=EntryType.RENT_CHARGE,
        description=f"Monthly rent — period starting {due_date}",
        idempotency_key=joint_rent_key(lease, due_date),
        metadata={"period_start": due_date.isoformat(), "joint": True},
    )


def generate_rent_charges_for_lease(
    lease: Lease, horizon_days: int = GENERATION_HORIZON_DAYS
) -> int:
    if lease.status != Lease.LeaseStatus.ACTIVE:
        return 0
    until = date.today() + timedelta(days=horizon_days)
    created = 0
    if lease_is_joint(lease):
        if not lease.lease_tenants.filter(declined=False).exists():
            return 0
        for due in _lease_rent_due_dates(lease, until):
            _, was_created = ensure_joint_rent_charge(lease, due)
            created += int(was_created)
        return created
    for lt in lease.lease_tenants.filter(tenant__isnull=False, declined=False):
        for due in _rent_due_dates(lt, until):
            _, was_created = ensure_rent_charge(lt, due)
            created += int(was_created)
    return created


def generate_initial_charges(lease: Lease) -> None:
    """
    Called once when the lease flips ACTIVE: deposits + rent +
    the rent schedule (first charge = prorated move-in period).
    Idempotent via natural keys.

    TWO DIFFERENT DUE DATES, deliberately:

      Rent      -> the tenancy start (you pay for August in August).

      Deposits  -> the day the agreement is entered into, i.e. NOW. A security
      and fees      deposit is not rent paid early; it is the consideration for
                    entering the agreement, and it is due on signing. Showing a
                    tenant who signed on July 12th that their deposit is
                    "Scheduled — due Aug 1" is simply false, and it is false in
                    the landlord's disfavour: it tells the tenant they may hold
                    the money for three more weeks.

                    `min(today, tenancy_start)` rather than a bare `today`, so a
                    lease activated retroactively (backdated paperwork) doesn't
                    end up with a deposit due AFTER the tenancy already began.

    JOINT leases: the security, pet, and lease-level cleaning deposits are
    HOUSEHOLD charges (tenant=None) — "their deposit is together". Only the
    explicitly individual per-tenant cleaning deposits stay per-tenant.
    SPLIT leases: one-time charges go to the primary tenant.
    """
    from django.utils import timezone

    tenancy_start = lease.move_in_date or lease.start_date
    today = timezone.now().date()
    one_time_due = min(today, tenancy_start)

    joint = lease_is_joint(lease)

    def _one_time(tenant, etype, amount, kind, note, *, joint_charge=False):
        if not amount or Decimal(amount) <= 0:
            return
        if not joint_charge and tenant is None:
            return
        key_owner = "joint" if joint_charge else tenant.pk
        post_charge(
            landlord=lease.landlord,
            tenant=None if joint_charge else tenant,
            lease=lease,
            amount=Decimal(amount),
            due_date=one_time_due,
            entry_type=etype,
            description=note,
            idempotency_key=f"{kind}:{lease.pk}:{key_owner}",
            metadata={
                "kind": kind,
                "due_on_signing": True,
                **({"joint": True} if joint_charge else {}),
            },
        )

    if joint:
        _one_time(
            None,
            EntryType.DEPOSIT_CHARGE,
            lease.security_deposit,
            "security_deposit",
            "Security deposit — due on signing",
            joint_charge=True,
        )
        _one_time(
            None,
            EntryType.DEPOSIT_CHARGE,
            lease.pet_deposit,
            "pet_deposit",
            "Pet damage deposit — due on signing",
            joint_charge=True,
        )
        _one_time(
            None,
            EntryType.DEPOSIT_CHARGE,
            lease.cleaning_deposit,
            "cleaning_deposit_lease",
            "Cleaning deposit — due on signing",
            joint_charge=True,
        )
    else:
        primary = (
            lease.lease_tenants.filter(
                is_primary_tenant=True, tenant__isnull=False
            ).first()
            or lease.lease_tenants.filter(tenant__isnull=False).first()
        )
        primary_tenant = primary.tenant if primary else None
        _one_time(
            primary_tenant,
            EntryType.DEPOSIT_CHARGE,
            lease.security_deposit,
            "security_deposit",
            "Security deposit — due on signing",
        )
        _one_time(
            primary_tenant,
            EntryType.DEPOSIT_CHARGE,
            lease.pet_deposit,
            "pet_deposit",
            "Pet damage deposit — due on signing",
        )
        _one_time(
            primary_tenant,
            EntryType.DEPOSIT_CHARGE,
            lease.cleaning_deposit,
            "cleaning_deposit_lease",
            "Cleaning deposit — due on signing",
        )

    # A per-tenant cleaning deposit is that person's own obligation in either mode
    # (it was negotiated individually on their LeaseTenant row).
    for lt in lease.lease_tenants.filter(
        tenant__isnull=False, cleaning_deposit__gt=0
    ):
        _one_time(
            lt.tenant,
            EntryType.DEPOSIT_CHARGE,
            lt.cleaning_deposit,
            "cleaning_deposit_individual",
            "Cleaning deposit (individual) — due on signing",
        )

    generate_rent_charges_for_lease(lease)


def stamp_deposit_received(charge: LedgerEntry) -> bool:
    """
    When a DEPOSIT_CHARGE is fully settled, record the date on the LEASE
    (Lease.security_deposit_received_date / pet_deposit_received_date /
    cleaning_deposit_received_date).

    Those fields already exist on AgreementTerms, already print on the
    agreement, and — until now — nothing ever set them. They are not decoration:
    the date the landlord RECEIVES a deposit is what starts the statutory clock
    for returning it at the end of the tenancy. A lease that says "Received on:
    Not yet received" three months into a tenancy is a document that will lose an
    RTB hearing.

    Called from the ledger.payment_posted handler. Idempotent.
    """
    if charge.entry_type != EntryType.DEPOSIT_CHARGE or not charge.lease_id:
        return False
    if charge.charge_status() != "PAID":
        return False

    kind = (charge.metadata or {}).get("kind")
    flipped = False
    if kind in {"cleaning_deposit_lease", "cleaning_deposit_individual"}:
        slots = charge.lease.lease_tenants.filter(declined=False)
        if kind == "cleaning_deposit_individual" and charge.tenant_id:
            slots = slots.filter(tenant_id=charge.tenant_id)
        flipped = bool(
            slots.filter(cleaning_deposit_paid=False).update(
                cleaning_deposit_paid=True
            )
        )

    field = {
        "security_deposit": "security_deposit_received_date",
        "pet_deposit": "pet_deposit_received_date",
        # Only the LEASE-level cleaning deposit gets a lease-level date. A
        # per-tenant one is that roommate's own money and stays on their
        # cleaning_deposit_paid flag — one date can't describe three roommates.
        "cleaning_deposit_lease": "cleaning_deposit_received_date",
    }.get(kind)
    if not field:
        return flipped

    lease = charge.lease
    if getattr(lease, field):
        return flipped  # already stamped — first receipt is the one that counts

    # The date the last dollar landed, not today: a deposit paid in two
    # instalments is "received" when it's whole.
    from .models import SETTLEMENT_TYPES

    last = (
        charge.settlements.filter(
            entry_type__in=SETTLEMENT_TYPES, reversed_by__isnull=True
        )
        .order_by("-effective_date")
        .first()
    )
    if not last:
        return flipped

    setattr(lease, field, last.effective_date)
    lease.save(update_fields=[field, "updated_at"])
    return True


# ------------------------------------------------------------ adjustments
def apply_adjustment_to_ledger(lt: LeaseTenant, adjustment) -> dict:
    """
    Reconcile existing rent charges with a newly created/changed adjustment.
    Never mutates a posted entry (see module docstring for the two paths).

    JOINT leases: the adjustment changes one roommate's share, so the
    HOUSEHOLD total for affected periods is recomputed and the joint
    charges are voided+reposted (unpaid) or credited/delta-charged (paid).
    """
    lease = lt.lease
    joint = lease_is_joint(lease)

    charges = LedgerEntry.objects.with_settlement().filter(
        lease=lease,
        entry_type=EntryType.RENT_CHARGE,
        reversed_by__isnull=True,
        due_date__gte=adjustment.effective_date,
    )
    charges = (
        charges.filter(tenant__isnull=True)
        if joint
        else charges.filter(tenant=lt.tenant)
    )
    if adjustment.end_date:
        charges = charges.filter(due_date__lte=adjustment.end_date)

    def _new_amount(due):
        return (
            compute_joint_rent_for_due_date(lease, due)
            if joint
            else compute_rent_for_due_date(lt, due)
        )

    def _key(due):
        base = joint_rent_key(lease, due) if joint else rent_key(lt, due)
        return f"{base}:adj{adjustment.pk}"

    charge_tenant = None if joint else lt.tenant
    reposted, credited = 0, 0
    for charge in charges:
        new_amount = _new_amount(charge.due_date)
        if new_amount == charge.amount:
            continue
        if charge.settled_amount == 0:
            void_entry(charge, reason=f"Rent adjustment #{adjustment.pk} applied")
            post_charge(
                landlord=lease.landlord,
                tenant=charge_tenant,
                lease=lease,
                amount=new_amount,
                due_date=charge.due_date,
                entry_type=EntryType.RENT_CHARGE,
                description=charge.description,
                # replaces the voided charge's natural slot
                idempotency_key=_key(charge.due_date),
                metadata={
                    **charge.metadata,
                    "adjustment_id": str(adjustment.pk),
                },
            )
            reposted += 1
        elif new_amount < charge.amount:
            post_credit(
                charge=charge,
                amount=charge.amount - new_amount,
                reason=f"Rent adjustment #{adjustment.pk}",
                idempotency_key=f"adjcredit:{adjustment.pk}:{charge.pk}",
                metadata={"adjustment_id": str(adjustment.pk)},
            )
            credited += 1
        # An INCREASE on a partially-paid month: charge the delta separately.
        else:
            post_charge(
                landlord=lease.landlord,
                tenant=charge_tenant,
                lease=lease,
                amount=new_amount - charge.amount,
                due_date=charge.due_date,
                entry_type=EntryType.RENT_CHARGE,
                description=f"Rent increase — period starting {charge.due_date}",
                idempotency_key=f"adjdelta:{adjustment.pk}:{charge.pk}",
                metadata={
                    "adjustment_id": str(adjustment.pk),
                    "delta_of": str(charge.pk),
                },
            )
            reposted += 1
    return {"reposted": reposted, "credited": credited}


# ------------------------------------------------------------ utilities
def compute_tenant_utility_portion(lease: Lease, bill_key: str | None, total: Decimal):
    """
    How much of a utility bill the TENANTS owe, per the lease's own
    bills_included configuration (the terms everyone signed):
      - included in rent        -> $0 (landlord's cost entirely)
      - tenant_responsibility:
          full        -> the whole bill
          percentage  -> value% of the bill
          fixed       -> the fixed $ value (capped at the bill total)
          none        -> $0
      - bill_key None / not configured on the lease -> the whole bill
        (a one-off the lease never contemplated — landlord decides by
        simply not posting it, or posting it in full).
    Returns (tenant_portion, meta) where meta lands in the charge metadata
    so the ledger shows WHY the tenant owes what they owe.
    """
    total = Decimal(total)
    info = (lease.bills_included or {}).get(bill_key) if bill_key else None
    if info is None:
        return total.quantize(Decimal("0.01")), {
            "bill_key": bill_key,
            "responsibility": "full",
            "configured": False,
        }
    meta = {
        "bill_key": bill_key,
        "provider": info.get("provider", ""),
        "configured": True,
    }
    if info.get("included", False):
        return Decimal("0.00"), {**meta, "responsibility": "included_in_rent"}
    resp = info.get("tenant_responsibility", {}) or {}
    rtype = resp.get("type", "none")
    if rtype == "full":
        portion = total
    elif rtype == "percentage":
        value = Decimal(str(resp.get("value", 0)))
        portion = total * value / Decimal("100")
        meta["percentage"] = str(value)
    elif rtype == "fixed":
        portion = min(Decimal(str(resp.get("value", 0))), total)
        meta["fixed_amount"] = str(resp.get("value", 0))
    else:  # "none"
        portion = Decimal("0.00")
    meta["responsibility"] = rtype
    return portion.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), meta


def split_utility_bill(
    *,
    lease: Lease,
    total_amount,
    period_start: date,
    period_end: date,
    description: str,
    due_date: date | None = None,
    created_by=None,
    record_landlord_expense: bool = False,
    expense_vendor: str = "",
    bill_key: str | None = None,
) -> list[LedgerEntry]:
    """
    JOINT leases: the household owes the household's bill — ONE joint
    UTILITY_CHARGE (tenant=None) for the full amount, payable by anyone.

    SPLIT leases: fan the bill into per-tenant UTILITY_CHARGE entries,
    weighted by days actually occupied during the billing period (temporal
    occupancy log), falling back to an equal split among current
    non-declined tenants when no occupancy rows exist yet.

    Optionally also books the landlord's own EXPENSE for the bill.
    """
    total = Decimal(total_amount).quantize(Decimal("0.01"))
    if total <= 0:
        raise ValueError("Bill amount must be positive.")
    due = due_date or period_end

    # The tenants owe only their configured share of the bill (the terms
    # they actually signed); the rest is the landlord's own cost, captured
    # by the optional EXPENSE below. A $200 bill on a lease that says
    # "tenant pays 50%" produces a $100 tenant charge, not $200.
    tenant_total, share_meta = compute_tenant_utility_portion(lease, bill_key, total)

    entries: list[LedgerEntry] = []

    if tenant_total <= 0:
        # Included in rent / responsibility "none": nothing to charge the
        # tenants — only the landlord's expense (if requested) gets booked.
        pass
    elif lease_is_joint(lease):
        if not lease.lease_tenants.filter(declined=False).exists():
            raise ValueError("No tenants to bill on this lease.")
        entry, _ = post_charge(
            landlord=lease.landlord,
            tenant=None,
            lease=lease,
            amount=tenant_total,
            due_date=due,
            entry_type=EntryType.UTILITY_CHARGE,
            description=f"{description} ({period_start} – {period_end})",
            idempotency_key=f"utility:{lease.pk}:joint:{period_start}:{period_end}:{description[:40]}",
            created_by=created_by,
            metadata={
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "bill_total": str(total),
                "tenant_share": str(tenant_total),
                "joint": True,
                **share_meta,
            },
        )
        entries.append(entry)
    else:
        from rentium.leases.occupancy import occupant_days_for_lease

        weights = occupant_days_for_lease(lease, period_start, period_end)
        if not weights:
            tenants = [
                lt.tenant
                for lt in lease.lease_tenants.filter(
                    tenant__isnull=False, declined=False
                )
            ]
            weights = {t: 1 for t in tenants}
        if not weights:
            raise ValueError("No tenants to bill on this lease.")
        total_weight = sum(weights.values())
        allocated = Decimal("0.00")
        items = list(weights.items())
        for i, (tenant, w) in enumerate(items):
            if i < len(items) - 1:
                share = (tenant_total * Decimal(w) / Decimal(total_weight)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                allocated += share
            else:
                share = (
                    tenant_total - allocated
                )  # last tenant absorbs rounding remainder
            if share <= 0:
                continue
            entry, _ = post_charge(
                landlord=lease.landlord,
                tenant=tenant,
                lease=lease,
                amount=share,
                due_date=due,
                entry_type=EntryType.UTILITY_CHARGE,
                description=f"{description} ({period_start} – {period_end})",
                idempotency_key=f"utility:{lease.pk}:{tenant.pk}:{period_start}:{period_end}:{description[:40]}",
                created_by=created_by,
                metadata={
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "bill_total": str(total),
                    "tenant_share": str(tenant_total),
                    "weight_days": w,
                    **share_meta,
                },
            )
            entries.append(entry)

    if record_landlord_expense:
        post_expense(
            landlord=lease.landlord,
            property=lease.property if lease.property_id else None,
            amount=total,
            category="UTILITIES",
            description=f"{description} ({period_start} – {period_end})",
            incurred_date=period_end,
            vendor=expense_vendor,
            idempotency_key=f"utilexp:{lease.pk}:{period_start}:{period_end}:{description[:40]}",
            created_by=created_by,
        )
    return entries


# ------------------------------------------------------------ lifecycle
def void_open_charges_for_lease(lease: Lease, *, reason: str, created_by=None) -> dict:
    """
    Ending a lease (terminate/expire) must also end its open receivables —
    otherwise the summary keeps counting dead leases' rent as "expected"
    and their charges sit Overdue forever. Voids every non-voided charge on
    the lease that has NO live settlements; anything with money already on
    it is skipped (received money is historical fact — resolve those by
    hand: void the payment first if the whole thing was a mistake).

    Call this from the lease `terminate` action (and the expiry task):

        from rentium.ledger.billing import void_open_charges_for_lease
        void_open_charges_for_lease(
            lease, reason=f"Lease {lease.lease_number} terminated",
            created_by=request.user,
        )

    Idempotent — already-voided charges are excluded by the filter, so
    calling it twice is harmless. The `void_lease_charges` management
    command wraps this for cleaning up historical data.
    """
    from .models import CHARGE_TYPES

    charges = LedgerEntry.objects.with_settlement().filter(
        lease=lease, entry_type__in=CHARGE_TYPES, reversed_by__isnull=True
    )
    voided, skipped = 0, 0
    for charge in charges:
        if charge.settled_amount and charge.settled_amount > 0:
            skipped += 1
            continue
        void_entry(charge, reason=reason, created_by=created_by)
        voided += 1
    return {"voided": voided, "skipped_with_payments": skipped}
