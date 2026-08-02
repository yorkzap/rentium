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
from decimal import InvalidOperation

from django.db import IntegrityError
from django.db import transaction

from rentium.events.registry import publish

from .models import CHARGE_TYPES
from .models import SETTLEMENT_TYPES
from .models import EntryType
from .models import LedgerEntry
from .models import PaymentMethod


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


def outstanding_on(charge: LedgerEntry) -> Decimal:
    """What is still owing on one charge, net of live settlements."""
    from django.db.models import Sum

    paid = charge.settlements.filter(
        entry_type__in=SETTLEMENT_TYPES, reversed_by__isnull=True
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    return charge.amount - paid


def suggest_deposit_split(candidates, amount) -> list | None:
    """Which open charges add up to exactly `amount`, if any obviously do.

    Deposits arrive together and land as one bank line: a tenant signs, sends a
    single $400 e-transfer, and that is $200 security + $200 cleaning. The
    charges stay separate all the way through (they are returned separately, and
    a cleaning deduction must never reach the security deposit), so the split has
    to happen at the moment the money is recorded.

    Exact full-pay of each selected charge only — a guess that leaves a stray
    $3 owing on a deposit is worse than no guess. Returns None when nothing
    matches cleanly, and the caller asks.
    """
    amount = Decimal(amount)
    if not candidates or amount <= 0:
        return None
    scored = []
    for charge in candidates:
        desc = (charge.description or "").casefold()
        kind = 0
        if "security" in desc or charge.entry_type == EntryType.DEPOSIT_CHARGE:
            kind = 3
        if "cleaning" in desc:
            kind = max(kind, 2)
        if "pet" in desc:
            kind = max(kind, 2)
        if "rent" in desc:
            kind = max(kind, 1)
        out = outstanding_on(charge)
        if out <= 0:
            continue
        scored.append((kind, out, charge))

    # Deposit-like charges first; fall back to everything open if there are none.
    pool = [(out, c) for kind, out, c in scored if kind >= 2] or [
        (out, c) for kind, out, c in scored
    ]
    pool = pool[:8]  # 2**8 subsets is the ceiling on an interactive path
    n = len(pool)
    best = None
    for mask in range(1, 1 << n):
        total = Decimal("0")
        chosen = []
        for i in range(n):
            if mask & (1 << i):
                total += pool[i][0]
                chosen.append(pool[i][1])
        if total == amount and 1 < len(chosen) <= 4:
            # Fewer charges first, then the more deposit-like reading.
            score = (
                -len(chosen),
                sum(1 for c in chosen if "deposit" in (c.description or "").casefold()),
            )
            if best is None or score > best[0]:
                best = (score, chosen)
    return best[1] if best else None


@transaction.atomic
def record_split_payment(
    *,
    landlord,
    allocations,
    payment_method,
    payment_date=None,
    reference_number="",
    notes="",
    paid_by=None,
    created_by=None,
    idempotency_prefix="split",
) -> list[tuple[LedgerEntry, bool]]:
    """One real-world payment, recorded against several charges.

    `allocations` is [(charge, amount)]. Posts one PAYMENT row per charge —
    append-only, one per charge, so each charge's own balance and its own
    settlement history stay true. There is deliberately no "combined payment"
    parent row: the ledger's unit is the settlement of a charge, and inventing a
    parent would give two places to ask "is this deposit paid?".

    All rows share a date, a method and a note naming the total, which is what
    ties them back to the single line on the bank statement.

    Returns [(entry, created)] in the order given, matching record_payment — a
    re-submitted split comes back created=False rather than double-posting.
    """
    parts = [(charge, Decimal(amount)) for charge, amount in allocations]
    if not parts:
        raise LedgerError("No charges to allocate this payment against.")
    if any(amount <= 0 for _, amount in parts):
        raise LedgerError("Every allocation must be a positive amount.")
    for charge, _ in parts:
        if charge.landlord_id != landlord.pk:
            raise LedgerError("That charge belongs to another landlord.")
    if len({charge.pk for charge, _ in parts}) != len(parts):
        raise LedgerError("The same charge appears twice in the allocation.")

    total = sum((amount for _, amount in parts), Decimal("0"))
    day = payment_date or date.today()

    posted = []
    for charge, amount in parts:
        posted.append(
            record_payment(
                charge=charge,
                amount=amount,
                payment_method=payment_method,
                payment_date=day,
                reference_number=reference_number,
                notes=(
                    notes or f"Allocated from ${total} {payment_method} (multi-charge)"
                ),
                paid_by=paid_by,
                created_by=created_by,
                idempotency_key=(
                    f"{idempotency_prefix}:{charge.pk}:{day}:{amount}:{total}"
                ),
            )
        )
    return posted


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
    _sync_source_document_payment_state(entry)

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
    _sync_source_document_payment_state(entry)
    return entry


def _sync_source_document_payment_state(entry: LedgerEntry) -> None:
    """Keep linked RamaDocument.payment_state aligned with ledger paid_on."""
    try:
        from rentium.rama.models import RamaDocument

        doc = getattr(entry, "source_document", None)
        if doc is None:
            doc = RamaDocument.objects.filter(ledger_entry_id=entry.pk).first()
        if doc is None:
            return
        wanted = (
            RamaDocument.PaymentState.PAID
            if entry.paid_on
            else RamaDocument.PaymentState.UNPAID
        )
        if doc.payment_state != wanted:
            doc.payment_state = wanted
            doc.save(update_fields=["payment_state", "updated_at"])
    except Exception:  # noqa: BLE001 — ledger must not fail if rama is mid-migrate
        return


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


_UNSET = object()


def reallocate_entry(
    entry: LedgerEntry,
    *,
    property=_UNSET,
    holding=_UNSET,
    reason,
    created_by=None,
) -> LedgerEntry:
    """
    Move a posted expense to the place it actually belongs — atomically, and
    linked to what it replaces.

    This exists because the alternative is improvisation. A shared-space repair
    booked against one room (the shower serving Rooms C, D and F) used to be
    "fixed" by voiding through one API and posting a fresh expense through
    another: three unrelated rows, nothing tying the replacement to the entry it
    replaced, and nothing stopping a fourth posting. correct_entry already does
    void-and-repost in one transaction, but it copies property/holding verbatim,
    so it could never express "same cost, different place".

    Two things make this more than a keyword argument on correct_entry:

    - It normalizes the scope PAIR. LedgerEntry.clean() rejects a property whose
      holding_id disagrees with the holding, so moving a room-scoped expense to
      the address has to null the property, and moving back has to re-derive the
      holding from the listing. Callers get that wrong; this doesn't.
    - It records that a reallocation happened, and from where — a typo fix and a
      cost moving between properties are different facts, and next year's tax
      summary needs to tell them apart.

    Append-only throughout: nothing is mutated, nothing is deleted, and the
    original plus its REVERSAL stay exactly where they are.
    """
    if entry.entry_type != EntryType.EXPENSE:
        raise LedgerError(
            "Only an expense can be reallocated. A charge's scope follows its "
            "lease — move the lease, or void the charge and raise a new one."
        )
    if entry.voided:
        raise LedgerError("That expense has already been voided.")

    new_property = entry.property if property is _UNSET else property
    new_holding = entry.holding if holding is _UNSET else holding

    # A listing implies its holding; the address alone means no listing.
    if new_property is not None:
        new_holding = new_property.holding
    if new_property is None and new_holding is None and holding is _UNSET:
        new_holding = None

    same_property = getattr(new_property, "pk", None) == entry.property_id
    same_holding = getattr(new_holding, "pk", None) == entry.holding_id
    if same_property and same_holding:
        raise LedgerError(
            "That expense is already booked there — nothing to reallocate."
        )

    def _scope(prop, hold):
        return {
            "property_id": str(prop.pk) if prop else None,
            "property_name": prop.name if prop else None,
            "holding_id": str(hold.pk) if hold else None,
            "holding_name": hold.name if hold else None,
        }

    # Capture receipt link before void (OneToOne may only point at the old row).
    source_doc = None
    try:
        from rentium.rama.models import RamaDocument

        source_doc = getattr(entry, "source_document", None)
        if source_doc is None:
            source_doc = RamaDocument.objects.filter(ledger_entry_id=entry.pk).first()
    except Exception:  # noqa: BLE001
        source_doc = None

    replacement = correct_entry(
        entry,
        created_by=created_by,
        reason=reason,
        property=new_property,
        holding=new_holding,
        metadata={
            **entry.metadata,
            "corrects": str(entry.id),
            "reallocated": {
                "from": _scope(entry.property, entry.holding),
                "to": _scope(new_property, new_holding),
                "reason": reason,
                "on": date.today().isoformat(),
            },
        },
    )

    if source_doc is not None:
        try:
            source_doc.ledger_entry = replacement
            if new_holding is not None:
                source_doc.holding = new_holding
            if new_property is not None:
                source_doc.property = new_property
            elif new_holding is not None:
                source_doc.property = None
            source_doc.save(
                update_fields=[
                    "ledger_entry",
                    "holding",
                    "property",
                    "updated_at",
                ]
            )
        except Exception:  # noqa: BLE001
            pass

    publish(
        "ledger.entry_reallocated",
        {
            "from_entry_id": str(entry.id),
            "to_entry_id": str(replacement.id),
            "from_scope": _scope(entry.property, entry.holding),
            "to_scope": _scope(new_property, new_holding),
            "reason": reason,
        },
        property_id=replacement.property_id,
    )
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
    effective_date=None,
    created_by=None,
    idempotency_key=None,
    metadata=None,
) -> tuple[LedgerEntry, bool]:
    return post_entry(
        landlord=landlord,
        property=property or (lease.property if lease and lease.property_id else None),
        lease=lease,
        tenant=tenant,
        entry_type=EntryType.DEPOSIT_RETURN,
        amount=Decimal(amount),
        effective_date=effective_date or date.today(),
        description=description,
        payment_method=payment_method,
        created_by=created_by,
        idempotency_key=idempotency_key,
        metadata=metadata or {},
    )


def refundable_deposit_balances(*, landlord, lease) -> list[dict]:
    """Return the refundable balance of each deposit charge on a lease.

    Incoming payments remain attached to their own deposit charge even when
    they originated in one bank transfer. Returns are likewise linked through
    immutable metadata, so security and cleaning deposits can be accounted for
    and returned separately.
    """
    from django.db.models import Sum

    labels = {
        "security_deposit": "Security deposit",
        "pet_deposit": "Pet damage deposit",
        "cleaning_deposit_lease": "Cleaning deposit",
        "cleaning_deposit_individual": "Cleaning deposit",
        # Read historical rows during/after the data migration as a safeguard.
        "cleaning_fee_lease": "Cleaning deposit",
        "cleaning_fee_individual": "Cleaning deposit",
    }
    returns = list(
        LedgerEntry.objects.not_voided().filter(
            landlord=landlord,
            lease=lease,
            entry_type=EntryType.DEPOSIT_RETURN,
        )
    )
    unallocated_returns = [
        row for row in returns if not (row.metadata or {}).get("source_charge_id")
    ]
    if unallocated_returns:
        raise LedgerError(
            "This lease has a historical deposit return that is not allocated "
            "to a deposit type. Allocate it before posting another automatic return."
        )

    sole_tenant = None
    tenant_ids = list(
        lease.lease_tenants.filter(tenant__isnull=False)
        .values_list("tenant_id", flat=True)
        .distinct()[:2]
    )
    if len(tenant_ids) == 1:
        from rentium.users.models import TenantProfile

        sole_tenant = TenantProfile.objects.filter(pk=tenant_ids[0]).first()

    balances = []
    charges = (
        LedgerEntry.objects.not_voided()
        .filter(
            landlord=landlord,
            lease=lease,
            entry_type=EntryType.DEPOSIT_CHARGE,
        )
        .select_related("tenant")
        .order_by("effective_date", "created_at")
    )
    for charge in charges:
        received = (
            charge.settlements.filter(
                entry_type=EntryType.PAYMENT,
                reversed_by__isnull=True,
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        returned = sum(
            (
                row.amount
                for row in returns
                if str((row.metadata or {}).get("source_charge_id"))
                == str(charge.pk)
            ),
            Decimal("0.00"),
        )
        balance = received - returned
        if balance <= 0:
            continue
        kind = str((charge.metadata or {}).get("kind") or "deposit")
        balances.append(
            {
                "charge": charge,
                "charge_id": str(charge.pk),
                "kind": kind,
                "label": labels.get(kind, "Deposit"),
                "received": received,
                "returned": returned,
                "balance": balance,
                "tenant": charge.tenant or sole_tenant,
            }
        )
    return balances


# The inspection records deductions against a deposit KIND ("the cleaning
# deposit"); the ledger holds them as charges. One kind can be several charges
# — a lease-level cleaning deposit plus per-roommate ones.
DEPOSIT_KIND_BY_LEDGER_KIND = {
    "security_deposit": "SECURITY",
    "pet_deposit": "PET",
    "cleaning_deposit_lease": "CLEANING",
    "cleaning_deposit_individual": "CLEANING",
    # Historical rows, same as the label map above.
    "cleaning_fee_lease": "CLEANING",
    "cleaning_fee_individual": "CLEANING",
}


def allocate_deductions(balances, totals) -> dict:
    """Spread an agreed per-deposit total across that deposit's charges.

    `totals` is {SECURITY|PET|CLEANING: Decimal} — what the tenant agreed to in
    writing. Returns {charge_id: Decimal}. Charges are filled in the order they
    were posted and each is capped at its own balance, so a deduction can never
    quietly reach into a different deposit: keeping cleaning money out of the
    security deposit is the whole reason they are separate charges.
    """
    remaining = {
        kind: Decimal(str(amount or 0))
        for kind, amount in (totals or {}).items()
        if Decimal(str(amount or 0)) > 0
    }
    allocated = {}
    for item in balances:
        deposit_kind = DEPOSIT_KIND_BY_LEDGER_KIND.get(item["kind"])
        left = remaining.get(deposit_kind, Decimal("0.00"))
        if left <= 0:
            continue
        take = min(left, item["balance"])
        allocated[item["charge_id"]] = take
        remaining[deposit_kind] = left - take

    over = {kind: amount for kind, amount in remaining.items() if amount > 0}
    if over:
        detail = ", ".join(f"{kind} by ${amount}" for kind, amount in over.items())
        raise LedgerError(
            f"The agreed deductions exceed the deposit money actually held "
            f"({detail}). A landlord can't keep more than they were given — "
            f"the balance is a debt to claim, not a deduction."
        )
    return allocated


@transaction.atomic
def return_refundable_deposits(
    *,
    landlord,
    lease,
    payment_method,
    effective_date=None,
    created_by=None,
    deductions=None,
) -> list[LedgerEntry]:
    """Return every held deposit as separate, idempotent ledger entries.

    `deductions` is {charge_id: Decimal} — money the tenant agreed IN WRITING
    that the landlord may keep (or that an RTB order awards). Callers must have
    checked that consent; this function books it, it does not authorise it.

    A deducted deposit posts THREE rows, not one netted row:

      1. DEPOSIT_RETURN for what the tenant actually gets back.
      2. DEPOSIT_RETURN for the deducted part, method OTHER — the money left
         the deposit liability but never left the bank. Without this leg
         `deposits_held` (payments in, minus DEPOSIT_RETURNs out) would show
         the landlord holding a deposit they have already spent, forever.
      3. An OTHER_CHARGE for the deduction plus a PAYMENT settling it from
         that money, so what was kept shows up as income that was earned
         rather than as a deposit that evaporated.

    Three explicit legs rather than one quiet subtraction, for the same reason
    damage claims are never netted against deposits: at a hearing, "we kept
    $187" has to be answerable line by line.
    """
    if payment_method not in PaymentMethod.values:
        raise LedgerError(f"Unknown deposit return method {payment_method!r}.")
    deductions = {str(k): Decimal(str(v)) for k, v in (deductions or {}).items()}

    posted = []
    for item in refundable_deposit_balances(landlord=landlord, lease=lease):
        charge_id = item["charge_id"]
        deducted = deductions.get(charge_id, Decimal("0.00"))
        if deducted < 0:
            raise LedgerError("A deduction cannot be negative.")
        if deducted > item["balance"]:
            raise LedgerError(
                f'The deduction on {item["label"]} (${deducted}) is more than '
                f'the ${item["balance"]} held against it.'
            )
        to_tenant = item["balance"] - deducted

        if to_tenant > 0:
            entry, _created = post_deposit_return(
                landlord=landlord,
                tenant=item["tenant"],
                amount=to_tenant,
                lease=lease,
                description=f'{item["label"]} returned',
                payment_method=payment_method,
                effective_date=effective_date,
                created_by=created_by,
                idempotency_key=f"deposit-return:{lease.pk}:{charge_id}:full",
                metadata={
                    "kind": item["kind"],
                    "source_charge_id": charge_id,
                    "returned_separately": True,
                    "leg": "returned_to_tenant",
                },
            )
            posted.append(entry)

        if deducted > 0:
            posted.extend(
                _post_agreed_deduction(
                    landlord=landlord,
                    lease=lease,
                    item=item,
                    amount=deducted,
                    effective_date=effective_date,
                    created_by=created_by,
                )
            )
    return posted


def _post_agreed_deduction(
    *, landlord, lease, item, amount, effective_date, created_by
) -> list[LedgerEntry]:
    """The two-and-a-half legs that turn held deposit money into kept money.

    Called only from return_refundable_deposits, which has already established
    that the tenant agreed to this in writing. See its docstring for why this
    is three rows rather than a subtraction.
    """
    charge_id = item["charge_id"]
    day = effective_date or date.today()
    label = item["label"]

    applied, _created = post_deposit_return(
        landlord=landlord,
        tenant=item["tenant"],
        amount=amount,
        lease=lease,
        description=f"{label} applied to agreed deductions",
        # Not the tenant's return method: this money never moved.
        payment_method=PaymentMethod.OTHER,
        effective_date=effective_date,
        created_by=created_by,
        idempotency_key=f"deposit-return:{lease.pk}:{charge_id}:applied",
        metadata={
            "kind": item["kind"],
            "source_charge_id": charge_id,
            "returned_separately": True,
            "leg": "applied_to_deductions",
        },
    )

    charge, _created = post_charge(
        landlord=landlord,
        tenant=item["tenant"],
        lease=lease,
        property=lease.property if lease.property_id else None,
        amount=amount,
        due_date=day,
        entry_type=EntryType.OTHER_CHARGE,
        description=f"Agreed deduction from the {label.lower()}",
        idempotency_key=f"deposit-deduction:{lease.pk}:{charge_id}",
        metadata={
            "kind": "deposit_deduction",
            "source_charge_id": charge_id,
            "deposit_kind": DEPOSIT_KIND_BY_LEDGER_KIND.get(item["kind"]),
        },
    )
    settlement, _created = record_payment(
        charge=charge,
        amount=amount,
        payment_method=PaymentMethod.OTHER,
        payment_date=day,
        notes=f"Settled from the {label.lower()} held on this lease",
        created_by=created_by,
        idempotency_key=f"deposit-deduction-paid:{lease.pk}:{charge_id}",
    )
    return [applied, charge, settlement]


def deposits_held(landlord) -> Decimal:
    """Payments settling DEPOSIT_CHARGEs, minus deposit returns (liability).

    Projected from `position.financial_position` rather than aggregated here:
    three functions used to compute this one quantity in three scopes, and the
    tenant-scoped one was wrong. See ledger/position.py for why they are now
    one computation.
    """
    from .position import Scope
    from .position import financial_position

    return financial_position(landlord, scope=Scope.portfolio()).deposits_held


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
    from .position import Scope
    from .position import financial_position

    held = financial_position(landlord, scope=Scope.of_lease(lease)).deposits_held

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

    # The real clock starts on the LATER of the tenancy ending and the
    # forwarding address arriving in writing, so it comes from the move-out
    # record when one exists. Falling back to the end date alone would report
    # a deadline that has not actually started — worse than reporting none.
    deadline = None
    clock_note = None
    move_out = getattr(lease, "moveout_requests", None)
    settlement = move_out.order_by("-created_at").first() if move_out else None
    if settlement is not None:
        status = settlement.deposit_status()
        deadline = status["deadline"]
        clock_note = status["blocked_on"] or status["what_must_happen"]
    elif ended:
        clock_note = (
            "No move-out record, so the 15-day clock cannot be computed — it "
            "starts when the tenant's forwarding address arrives in writing."
        )

    return {
        "deposit_held": str(held),
        "claimed": str(outstanding),
        "claims": claims,
        # What is left IF every claim were agreed in writing. Not an
        # entitlement — see lawful_routes.
        "returnable_if_all_claims_agreed": str(max(held - outstanding, Decimal("0.00"))),
        "tenancy_ended": ended.isoformat() if ended else None,
        "claim_deadline": deadline,
        "clock_note": clock_note,
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


def tenant_statement(landlord, *, tenant, lease=None) -> dict:
    """Everything one tenant owes and has paid, in one place.

    "What does this tenant owe?" spans rent, utilities, damage claims and the
    deposit, and nothing assembled it — a landlord had to read three screens
    and add up. Joint charges are included because on a roommate lease each
    tenant is liable for the whole household charge, not a share of it; the
    flag says which is which so a conversation about "your rent" is accurate.
    """
    from .position import Scope
    from .position import charges_in_scope
    from .position import financial_position

    # ONE scope predicate for the rows, the totals and the deposit alike.
    #
    # This function used to build the joint-aware predicate here for its
    # `charges` list and then ignore it thirty lines below, aggregating the
    # deposit on `tenant=tenant` — which a joint-lease deposit payment never
    # carries. The result was a payload that contradicted itself:
    # `paid_to_date: "100.00"` beside `deposit_held: "0.00"`, and a NEGATIVE
    # deposit once any of it was returned.
    scope = Scope.of_tenant(tenant, lease=lease)
    position = financial_position(landlord, scope=scope)

    charges, owed, paid = [], Decimal("0.00"), Decimal("0.00")
    for entry in (
        charges_in_scope(landlord, scope=scope)
        .select_related("work_order", "lease")
        .order_by("due_date")
    ):
        outstanding = entry.outstanding or Decimal("0.00")
        settled = entry.settled_amount or Decimal("0.00")
        owed += outstanding
        paid += settled
        charges.append(
            {
                "id": str(entry.id),
                "type": entry.get_entry_type_display(),
                "description": entry.description,
                "due_date": entry.due_date.isoformat() if entry.due_date else None,
                "amount": str(entry.amount),
                "outstanding": str(outstanding),
                # charge_status is a method, not a property.
                "status": entry.charge_status(),
                "is_joint": entry.tenant_id is None,
                "is_damage": bool(entry.work_order_id),
                "lease": entry.lease.lease_number if entry.lease_id else None,
            }
        )

    damage = sum(
        (Decimal(c["outstanding"]) for c in charges if c["is_damage"]),
        Decimal("0.00"),
    )
    return {
        "tenant": tenant.user.name,
        "owes_now": str(owed),
        "of_which_damage": str(damage),
        "paid_to_date": str(paid),
        "deposit_held": str(position.deposits_held),
        "charges": charges,
        # Stated explicitly so nobody reads owes_now against deposit_held and
        # treats the difference as settled. See deposit_position().
        "note": (
            "Deposit money is held separately and is NOT netted off what is "
            "owed. Keeping any of it needs the tenant's written agreement or "
            "an RTB application within 15 days of the tenancy ending."
        ),
    }


# --------------------------------------------------------- duplicate guard
# How far apart two records of the same cost can plausibly be. A receipt is
# usually photographed within a couple of weeks of the spend being mentioned,
# and a statement line can lag the purchase date by about that much.
DUPLICATE_WINDOW_DAYS = 14


def find_duplicate_expense_candidates(
    landlord,
    *,
    amount,
    on_date=None,
    property=None,
    holding=None,
    exclude_entry_id=None,
    window_days: int = DUPLICATE_WINDOW_DAYS,
) -> list[dict]:
    """Existing expenses that might already be this same cost.

    Two paths can record one expense and neither could see the other: the chat
    path (rama.create_expense) writes with no idempotency key, and the document
    path (rama.document_services.file_document) keys only on its own document
    id. The sha256 check on upload catches the same FILE twice — it cannot
    catch the same COST arriving once by message and once by receipt.

    Deliberately advisory: this returns candidates for a human to judge and
    never blocks or merges on its own. Matching on an exact amount inside a
    window is narrow enough to stay quiet on genuinely repeated costs of
    different sizes, and a same-amount recurring cost (a monthly fee) is
    exactly the case where a person should confirm rather than a heuristic.
    """
    from datetime import timedelta

    from .models import EntryType, LedgerEntry

    try:
        amt = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return []

    day = on_date or date.today()
    qs = (
        LedgerEntry.objects.not_voided()
        .filter(
            landlord=landlord,
            entry_type=EntryType.EXPENSE,
            amount=amt,
            effective_date__gte=day - timedelta(days=window_days),
            effective_date__lte=day + timedelta(days=window_days),
        )
        .select_related("property", "holding")
        .order_by("-effective_date")
    )
    if exclude_entry_id:
        qs = qs.exclude(pk=exclude_entry_id)

    out = []
    for entry in qs[:5]:
        # Same scope is a stronger signal, but a mismatch is not a reason to
        # stay silent: recording a cost portfolio-wide and then filing its
        # receipt against the holding is a common way to double up.
        same_scope = (
            (property is not None and entry.property_id == property.pk)
            or (holding is not None and entry.holding_id == holding.pk)
            or (property is None and holding is None)
        )
        out.append(
            {
                "id": str(entry.pk),
                "amount": str(entry.amount),
                "description": entry.description,
                "effective_date": entry.effective_date.isoformat()
                if entry.effective_date
                else None,
                "scope": (
                    entry.property.name
                    if entry.property_id
                    else (entry.holding.name if entry.holding_id else "portfolio-wide")
                ),
                "same_scope": same_scope,
                "has_document": hasattr(entry, "source_document"),
                "days_apart": abs((entry.effective_date - day).days)
                if entry.effective_date
                else None,
            }
        )
    return out
