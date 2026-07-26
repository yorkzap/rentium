"""
Service layer: every ledger write in the app goes through these functions,
so idempotency, event publishing, and validation happen in exactly one place.
Views, Celery tasks, and (later) the AI controller all call the same code.

What is deliberately NOT here: reactions. Recording that money arrived is a fact
and must commit; stamping a date on a lease because money arrived is a
consequence and must be allowed to fail without taking the fact down with it.
Consequences live in handlers.py, downstream of the outbox.
"""

from datetime import date
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError
from django.db import transaction

from rentium.events.registry import publish

from .models import CHARGE_TYPES
from .models import SETTLEMENT_TYPES
from .models import EntryType
from .models import LedgerEntry


class LedgerError(Exception):
    pass


def _get_by_idempotency_key(key):
    if not key:
        return None
    return LedgerEntry.objects.filter(idempotency_key=key).first()


def post_entry(**fields) -> tuple[LedgerEntry, bool]:
    """
    Post one entry. Returns (entry, created). If the idempotency key already
    exists, the ORIGINAL entry is returned with created=False — a duplicate
    submission is a successful no-op, never an error and never a double-post.
    """
    key = fields.get("idempotency_key")
    prop = fields.get("property")
    if prop is not None and fields.get("holding") is None:
        fields["holding"] = prop.holding
    existing = _get_by_idempotency_key(key)
    if existing:
        return existing, False
    try:
        with transaction.atomic():
            entry = LedgerEntry(**fields)
            entry.save()
    except IntegrityError:
        # Raced with a concurrent identical submission — return the winner.
        existing = _get_by_idempotency_key(key)
        if existing:
            return existing, False
        raise
    return entry, True


def post_charge(
    *,
    landlord,
    tenant,
    amount,
    due_date,
    entry_type=EntryType.OTHER_CHARGE,
    lease=None,
    property=None,
    holding=None,
    description="",
    idempotency_key=None,
    created_by=None,
    metadata=None,
    work_order=None,
) -> tuple[LedgerEntry, bool]:
    """tenant=None with a lease = joint household charge (roommate leases)."""
    if entry_type not in CHARGE_TYPES:
        raise LedgerError(f"{entry_type} is not a charge type.")
    if tenant is None and lease is None:
        raise LedgerError("A charge needs a tenant, or a lease for a joint charge.")

    entry, created = post_entry(
        landlord=landlord,
        property=property or (lease.property if lease and lease.property_id else None),
        holding=holding,
        lease=lease,
        tenant=tenant,
        entry_type=entry_type,
        amount=Decimal(amount),
        due_date=due_date,
        effective_date=due_date,
        description=description,
        idempotency_key=idempotency_key,
        created_by=created_by,
        metadata=metadata or {},
        work_order=work_order,
    )
    if created:
        publish(
            "ledger.charge_posted",
            {
                "entry_id": str(entry.id),
                "entry_type": entry.entry_type,
                "amount": str(entry.amount),
                "due_date": str(entry.due_date),
                "tenant_id": str(tenant.pk) if tenant else None,
                "joint": tenant is None,
            },
            property_id=entry.property_id,
            lease_id=entry.lease_id,
        )
    return entry, created


def record_payment(
    *,
    charge: LedgerEntry,
    amount,
    payment_method,
    payment_date=None,
    reference_number="",
    idempotency_key=None,
    created_by=None,
    notes="",
    paid_by=None,
) -> tuple[LedgerEntry, bool]:
    """
    Landlord confirms money received (e-transfer arrived, cash in hand).
    Money never moves through the app — this is the book entry.

    `paid_by` (TenantProfile) records WHO paid. It matters most on joint
    household charges (charge.tenant is None): either roommate can send
    $400 — or the full $800 — and each payment still names its payer, so
    the household balance clears collectively while who-paid-what stays
    on the record. Defaults to charge.tenant for split-billing charges.
    """
    if charge.entry_type not in CHARGE_TYPES:
        raise LedgerError("Payments must be recorded against a charge.")
    if charge.voided:
        raise LedgerError("This charge has been voided; post a new charge first.")

    amount = Decimal(amount)
    if amount <= 0:
        raise LedgerError("Payment amount must be positive.")

    payer = paid_by or charge.tenant
    entry, created = post_entry(
        landlord=charge.landlord,
        property=charge.property,
        lease=charge.lease,
        tenant=payer,
        entry_type=EntryType.PAYMENT,
        amount=amount,
        effective_date=payment_date or date.today(),
        description=notes or f"Payment toward: {charge.description}",
        settles=charge,
        payment_method=payment_method,
        reference_number=reference_number,
        idempotency_key=idempotency_key,
        created_by=created_by,
    )
    if created:
        publish(
            "ledger.payment_posted",
            {
                "entry_id": str(entry.id),
                "charge_id": str(charge.id),
                "amount": str(amount),
                "method": payment_method,
                "paid_by": str(payer.pk) if payer else None,
                # charge_status() is recomputed live, so this reflects the state
                # AFTER this payment — which is what the deposit-stamp handler
                # downstream needs to decide whether the deposit is now whole.
                "charge_status": charge.charge_status(),
            },
            property_id=entry.property_id,
            lease_id=entry.lease_id,
        )
    return entry, created


def post_credit(
    *,
    charge: LedgerEntry,
    amount,
    reason,
    idempotency_key=None,
    created_by=None,
    metadata=None,
) -> tuple[LedgerEntry, bool]:
    """A discount/goodwill credit against a charge. The charge keeps its
    original amount (audit truth: 'rent was $1,300, landlord credited $200')."""
    if charge.entry_type not in CHARGE_TYPES:
        raise LedgerError("Credits apply to charges.")
    if charge.voided:
        raise LedgerError("This charge has been voided.")

    amount = Decimal(amount)
    if amount <= 0:
        raise LedgerError("Credit amount must be positive.")

    entry, created = post_entry(
        landlord=charge.landlord,
        property=charge.property,
        lease=charge.lease,
        tenant=charge.tenant,
        entry_type=EntryType.CREDIT,
        amount=amount,
        effective_date=date.today(),
        description=reason or f"Credit toward: {charge.description}",
        settles=charge,
        idempotency_key=idempotency_key,
        created_by=created_by,
        metadata=metadata or {},
    )
    if created:
        publish(
            "ledger.credit_posted",
            {
                "entry_id": str(entry.id),
                "charge_id": str(charge.id),
                "amount": str(amount),
            },
            property_id=entry.property_id,
            lease_id=entry.lease_id,
        )
    return entry, created


def post_expense(
    *,
    landlord,
    amount,
    category,
    description,
    incurred_date=None,
    property=None,
    holding=None,
    vendor="",
    work_order=None,
    idempotency_key=None,
    created_by=None,
    metadata=None,
    paid_on=None,
) -> tuple[LedgerEntry, bool]:
    """
    Money out.

    `paid_on` is optional and means "this has already cleared my bank". Leave it
    None — the common case, since providers auto-debit on their own schedule —
    and the expense shows as "Not yet taken from bank" until you say otherwise.
    An expense you've recorded but not yet paid is a real, useful state: it's the
    difference between what you owe and what you've spent.
    """
    entry, created = post_entry(
        landlord=landlord,
        property=property,
        holding=holding,
        entry_type=EntryType.EXPENSE,
        amount=Decimal(amount),
        effective_date=incurred_date or date.today(),
        description=description,
        category=category,
        vendor=vendor,
        work_order=work_order,
        idempotency_key=idempotency_key,
        created_by=created_by,
        metadata=metadata or {},
        paid_on=paid_on,
    )
    if created:
        publish(
            "ledger.expense_posted",
            {
                "entry_id": str(entry.id),
                "amount": str(entry.amount),
                "category": category,
                "paid_on": str(entry.paid_on) if entry.paid_on else None,
            },
            property_id=entry.property_id,
        )
    return entry, created


def mark_expense_paid(
    entry: LedgerEntry, *, paid_on=None, created_by=None
) -> LedgerEntry:
    """
    "The money has now actually left my account."

    The ONLY function in the app that mutates a posted entry, and it moves exactly
    one whitelisted column (LedgerEntry.MUTABLE_AFTER_POST). Everything that says
    what the expense WAS stays as immutable as it ever was.
    """
    if entry.entry_type != EntryType.EXPENSE:
        raise LedgerError("Only an expense has a bank-clearing date.")
    if entry.voided:
        raise LedgerError("This expense has been voided.")

    when = paid_on or date.today()
    if when > date.today():
        raise LedgerError("Money can't have left your account in the future.")

    entry.paid_on = when
    entry.save(update_fields=["paid_on"])

    publish(
        "ledger.expense_settled",
        {"entry_id": str(entry.id), "paid_on": str(entry.paid_on)},
        property_id=entry.property_id,
    )
    return entry


def unmark_expense_paid(entry: LedgerEntry) -> LedgerEntry:
    """Back to 'not yet taken from bank'. A mis-click is a mis-click, not a void."""
    if entry.entry_type != EntryType.EXPENSE:
        raise LedgerError("Only an expense has a bank-clearing date.")
    entry.paid_on = None
    entry.save(update_fields=["paid_on"])
    return entry


def void_entry(entry: LedgerEntry, *, reason, created_by=None) -> LedgerEntry:
    """
    Void = post an equal-and-opposite REVERSAL. Rules:
    - a charge with live (non-voided) settlements can't be voided —
      void the payments first, so money received is never orphaned;
    - voiding a payment/credit automatically 'reopens' the charge,
      because computed status ignores voided settlements.
    """
    if entry.voided:
        raise LedgerError("Entry is already voided.")
    if entry.entry_type == EntryType.REVERSAL:
        raise LedgerError("Reversals cannot be voided.")

    if entry.entry_type in CHARGE_TYPES:
        live = entry.settlements.filter(
            entry_type__in=SETTLEMENT_TYPES, reversed_by__isnull=True
        ).exists()
        if live:
            raise LedgerError(
                "This charge has payments/credits recorded against it. Void those first."
            )

    with transaction.atomic():
        reversal = LedgerEntry(
            landlord=entry.landlord,
            property=entry.property,
            holding=entry.holding,
            lease=entry.lease,
            tenant=entry.tenant,
            entry_type=EntryType.REVERSAL,
            amount=entry.amount,
            effective_date=date.today(),
            description=f"VOID: {entry.description} — {reason}",
            reverses=entry,
            created_by=created_by,
            metadata={"reason": reason},
        )
        reversal.save()

    publish(
        "ledger.entry_voided",
        {"entry_id": str(entry.id), "reversal_id": str(reversal.id), "reason": reason},
        property_id=entry.property_id,
        lease_id=entry.lease_id,
    )
    return reversal


def correct_entry(
    entry: LedgerEntry, *, created_by=None, reason="Correction", **new_fields
) -> LedgerEntry:
    """
    The 'edit' the frontend exposes: atomically void the old entry and post
    a corrected copy. Painless typo fix for the landlord, intact audit trail
    for everyone else.
    """
    with transaction.atomic():
        void_entry(entry, reason=reason, created_by=created_by)
        data = {
            "landlord": entry.landlord,
            "property": entry.property,
            "holding": entry.holding,
            "lease": entry.lease,
            "tenant": entry.tenant,
            "entry_type": entry.entry_type,
            "amount": entry.amount,
            "due_date": entry.due_date,
            "effective_date": entry.effective_date,
            "description": entry.description,
            "settles": entry.settles,
            "payment_method": entry.payment_method,
            "reference_number": entry.reference_number,
            "category": entry.category,
            "vendor": entry.vendor,
            "work_order": entry.work_order,
            # Carried, not reset: correcting a typo in the description of a bill
            # you already paid must not un-pay it.
            "paid_on": entry.paid_on,
            "metadata": {**entry.metadata, "corrects": str(entry.id)},
            "created_by": created_by,
        }
        data.update(new_fields)
        data.pop("idempotency_key", None)  # a correction is a new fact
        replacement = LedgerEntry(**data)
        replacement.save()
    return replacement


def post_deposit_return(
    *,
    landlord,
    tenant,
    amount,
    lease=None,
    property=None,
    description="Deposit returned",
    payment_method="ETRANSFER",
    created_by=None,
    idempotency_key=None,
) -> tuple[LedgerEntry, bool]:
    return post_entry(
        landlord=landlord,
        property=property or (lease.property if lease and lease.property_id else None),
        lease=lease,
        tenant=tenant,
        entry_type=EntryType.DEPOSIT_RETURN,
        amount=Decimal(amount),
        effective_date=date.today(),
        description=description,
        payment_method=payment_method,
        created_by=created_by,
        idempotency_key=idempotency_key,
    )


def deposits_held(landlord) -> Decimal:
    """Payments settling DEPOSIT_CHARGEs, minus deposit returns (liability)."""
    from django.db.models import Sum

    received = LedgerEntry.objects.not_voided().filter(
        landlord=landlord,
        entry_type=EntryType.PAYMENT,
        settles__entry_type=EntryType.DEPOSIT_CHARGE,
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    returned = LedgerEntry.objects.not_voided().filter(
        landlord=landlord, entry_type=EntryType.DEPOSIT_RETURN
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    return received - returned


def deposits_collected_between(landlord, start, end, property_id=None) -> Decimal:
    """
    Deposit payments RECEIVED in [start, end) — inflow, not net liability.

    Deposits stay excluded from income (they're refundable), but a landlord
    looking at "collected $0 this month" after banking a $425 deposit is
    being told something false. This is the number that fixes that.
    """
    from django.db.models import Sum

    qs = LedgerEntry.objects.not_voided().filter(
        landlord=landlord,
        entry_type=EntryType.PAYMENT,
        settles__entry_type=EntryType.DEPOSIT_CHARGE,
        effective_date__gte=start,
        effective_date__lt=end,
    )
    if property_id:
        qs = qs.filter(property_id=property_id)
    return qs.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")


def next_upcoming_charge(landlord, property_id=None) -> dict | None:
    """
    The earliest not-yet-settled charge with a future due date, or None.

    Lets the dashboard say "next charge: Aug 1 — $850" instead of showing a
    dead $0 month when the tenancy simply hasn't started billing yet.
    """
    from datetime import date

    from .models import CHARGE_TYPES

    qs = (
        LedgerEntry.objects.with_settlement()
        .filter(
            landlord=landlord,
            entry_type__in=CHARGE_TYPES,
            reversed_by__isnull=True,
            due_date__gt=date.today(),
            outstanding__gt=0,
        )
        .select_related("property")
        .order_by("due_date", "id")
    )
    if property_id:
        qs = qs.filter(property_id=property_id)

    charge = qs.first()
    if charge is None:
        return None
    return {
        "due_date": charge.due_date.isoformat(),
        "amount": str(charge.amount),
        "entry_type": charge.entry_type,
        # Stringify: RAMA audit / JSONField cannot store raw UUID objects.
        "lease_id": str(charge.lease_id) if charge.lease_id else None,
        "property_name": charge.property.name if charge.property else "",
    }


# ----------------------------------------------------- deposits vs claims
# BC RTA: a landlord may NOT keep any part of a deposit without the tenant's
# written agreement or an RTB dispute-resolution application filed within 15
# days of the later of (tenancy end, receiving the forwarding address). Getting
# it wrong costs DOUBLE the deposit.
#
# So damage never auto-deducts. It raises a CLAIM the tenant owes, and the
# deposit stays a separate liability until the landlord does one of the two
# lawful things. This function reports the position; it decides nothing.
DEPOSIT_CLAIM_WINDOW_DAYS = 15


def deposit_position(landlord, *, lease) -> dict:
    """Deposit held for a lease, what is claimed against it, and the deadline.

    Returns plain numbers plus `lawful_routes` — the only two ways the money
    can actually be kept — so no caller is tempted to net one off the other and
    call it settled.
    """
    from django.db.models import Sum

    held = LedgerEntry.objects.not_voided().filter(
        landlord=landlord,
        lease=lease,
        entry_type=EntryType.PAYMENT,
        settles__entry_type=EntryType.DEPOSIT_CHARGE,
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    returned = LedgerEntry.objects.not_voided().filter(
        landlord=landlord, lease=lease, entry_type=EntryType.DEPOSIT_RETURN
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    held = held - returned

    # Everything still owed on this lease — damage claims and unpaid rent alike.
    claims = []
    outstanding = Decimal("0.00")
    for entry in (
        # `outstanding` is a with_settlement() annotation, not a field.
        LedgerEntry.objects.with_settlement()
        .filter(
            landlord=landlord,
            lease=lease,
            entry_type__in=CHARGE_TYPES,
            reversed_by__isnull=True,
        )
        .exclude(entry_type=EntryType.DEPOSIT_CHARGE)
        .select_related("work_order")
    ):
        owing = entry.outstanding or Decimal("0.00")
        if owing <= 0:
            continue
        outstanding += owing
        claims.append(
            {
                "description": entry.description,
                "amount": str(owing),
                "is_damage": bool(entry.work_order_id),
                "work_order": str(entry.work_order_id) if entry.work_order_id else None,
            }
        )

    ended = lease.move_out_date or lease.end_date
    deadline = None
    if ended:
        deadline = ended + timedelta(days=DEPOSIT_CLAIM_WINDOW_DAYS)

    return {
        "deposit_held": str(held),
        "claimed": str(outstanding),
        "claims": claims,
        # What is left IF every claim were agreed in writing. Not an
        # entitlement — see lawful_routes.
        "returnable_if_all_claims_agreed": str(max(held - outstanding, Decimal("0.00"))),
        "tenancy_ended": ended.isoformat() if ended else None,
        "claim_deadline": deadline.isoformat() if deadline else None,
        "lawful_routes": [
            "the tenant agrees IN WRITING to the amount being kept, or",
            "you apply for RTB dispute resolution within 15 days of the later "
            "of the tenancy ending and receiving their forwarding address.",
        ],
        "warning": (
            "Do not deduct unilaterally. Missing the 15-day route means the "
            "claim is lost AND double the deposit becomes payable."
        ),
    }
