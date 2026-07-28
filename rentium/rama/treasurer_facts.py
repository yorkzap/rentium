"""
Financial facts the ledger does not have — and reconciling them against it.

The motivating case: "you're missing that we took $2,000 rent from another
tenant for a year, which you haven't factored in." That is true, material, and
unrecorded. But the naive version of remembering it is dangerous — if any of
that rent WAS in the ledger, adding the assertion on top double-counts it, and
a double-counted income figure is worse than a missing one because it looks
like good news.

So every fact is reconciled against the ledger at write time. Where the books
already hold most of what was asserted, the fact is still recorded and still
shown — but excluded from totals, with the overlap stated. The landlord is
never silently overruled and never silently double-counted.

Shape mirrors memory.py deliberately: rejects() / write() / active_*() /
render_for_pack(). Same discipline, different lifecycle.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import TreasurerFact

# Above this share of the assertion already present in the ledger, adding it
# again is more likely to be double-counting than a genuine gap. Not a
# certainty — which is why the fact is kept and flagged rather than refused.
DOUBLE_COUNT_THRESHOLD = Decimal("0.80")

# ------------------------------------------------------------------ guards
def rejects(statement: str, *, value_numeric=None) -> str | None:
    """Reason to refuse recording this, or None.

    Narrower than memory.rejects() on purpose: numbers are the POINT here, so
    the money/date/count refusals that protect RamaMemory would refuse
    everything useful. What is refused is only what cannot be stored safely.

    Note there is deliberately NO requirement for a property scope. A
    portfolio-wide fact ("we spent $5,000 on accounting last year") is
    perfectly reconcilable — an absent scope filter simply means all of this
    landlord's entries. Requiring a holding would refuse legitimate facts.
    Whether a per-period amount has a date range IS required, and is checked
    where the dates are parsed, so the message can name the missing fields.
    """
    from .memory import _SPECIAL_CATEGORY  # one definition of this, not two

    text = (statement or "").strip()
    if not text:
        return "There's nothing to record — what should I take into account?"
    if len(text) > TreasurerFact.MAX_STATEMENT_CHARS:
        return (
            "That's too long for one fact. Give me the single figure and what "
            f"it covers, in {TreasurerFact.MAX_STATEMENT_CHARS} characters or fewer."
        )
    if _SPECIAL_CATEGORY.search(text):
        return (
            "I can't store that. It describes someone's health, background, or "
            "personal circumstances, and keeping a standing financial record of "
            "it would put you offside privacy law (PIPEDA / BC PIPA)."
        )
    return None


def normalise_key(subject: str) -> str:
    return (slugify(subject or "")[:120]).strip("-")


def personal_data_present(landlord, statement: str) -> bool:
    from .memory import personal_data_present as _shared

    return _shared(landlord, statement)


# ------------------------------------------------------------- reconciliation
def _months_covered(fact) -> Decimal:
    """How many months the assertion spans, for a per-period figure."""
    if fact.period == TreasurerFact.Period.ONE_TIME:
        return Decimal("1")
    if not fact.effective_from or not fact.effective_to:
        return Decimal("1")
    days = (fact.effective_to - fact.effective_from).days
    return max(Decimal(days) / Decimal("30.44"), Decimal("1"))


def asserted_total(fact) -> Decimal | None:
    """The whole amount the assertion implies over its period."""
    if fact.value_numeric is None:
        return None
    amount = Decimal(fact.value_numeric)
    if fact.period == TreasurerFact.Period.MONTHLY:
        return (amount * _months_covered(fact)).quantize(Decimal("0.01"))
    if fact.period == TreasurerFact.Period.ANNUAL:
        return (amount * _months_covered(fact) / Decimal("12")).quantize(Decimal("0.01"))
    return amount.quantize(Decimal("0.01"))


def reconcile(fact) -> dict:
    """How much of this assertion the ledger already holds.

    This is the guard against a correction becoming a double-count. Compares
    like with like: an income assertion against income actually recorded in the
    same scope and window, an expense assertion against expenses.
    """
    from django.db.models import Sum

    from rentium.ledger.models import EntryType, LedgerEntry

    total = asserted_total(fact)
    if total is None or fact.direction == TreasurerFact.Direction.NEUTRAL:
        fact.ledger_overlap_amount = None
        fact.double_count_risk = False
        fact.reconciled_at = timezone.now()
        return {"overlap": None, "risk": False}

    rows = LedgerEntry.objects.not_voided().filter(landlord=fact.landlord)
    if fact.lease_id:
        rows = rows.filter(lease_id=fact.lease_id)
    elif fact.property_id:
        rows = rows.filter(property_id=fact.property_id)
    elif fact.holding_id:
        rows = rows.filter(holding_id=fact.holding_id)

    if fact.direction == TreasurerFact.Direction.INCOME:
        # What actually ARRIVED, not what was billed — "we took $2,000 rent"
        # is a claim about money received, so a charge that was never paid is
        # not evidence against it.
        rows = rows.filter(entry_type=EntryType.PAYMENT)
    else:
        rows = rows.filter(entry_type=EntryType.EXPENSE)
        if fact.category:
            rows = rows.filter(category=fact.category)

    # Both sides date from effective_date: for a payment that is when it
    # landed, for an expense when it was incurred.
    if fact.effective_from:
        rows = rows.filter(effective_date__gte=fact.effective_from)
    if fact.effective_to:
        rows = rows.filter(effective_date__lte=fact.effective_to)

    overlap = rows.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    risk = bool(total) and (overlap / total) >= DOUBLE_COUNT_THRESHOLD

    fact.ledger_overlap_amount = overlap
    fact.double_count_risk = risk
    fact.reconciled_at = timezone.now()
    return {"overlap": overlap, "risk": risk, "asserted": total}


# ------------------------------------------------------------------- writes
@transaction.atomic
def write(
    landlord,
    *,
    key: str,
    subject: str,
    statement: str,
    kind: str = TreasurerFact.Kind.LANDLORD_ASSERTED,
    confidence: str = TreasurerFact.Confidence.STATED,
    direction: str = TreasurerFact.Direction.NEUTRAL,
    value_numeric=None,
    value_unit: str = "CAD",
    period: str = TreasurerFact.Period.ONE_TIME,
    effective_from=None,
    effective_to=None,
    holding=None,
    property=None,
    lease=None,
    category: str = "",
    source_type: str = "LANDLORD",
    source_conversation=None,
    source_document=None,
    created_by_role: str = "landlord",
) -> TreasurerFact:
    """Record a fact, superseding any active one on the same key.

    Never edits: the previous row is SUPERSEDED and pointed at by the new one,
    so a corrected belief leaves a trail rather than vanishing.
    """
    slug = normalise_key(key)
    previous = (
        TreasurerFact.objects.select_for_update()
        .filter(landlord=landlord, key=slug, status=TreasurerFact.Status.ACTIVE)
        .first()
    )
    if previous is not None:
        previous.status = TreasurerFact.Status.SUPERSEDED
        previous.save(update_fields=["status"])

    fact = TreasurerFact(
        landlord=landlord,
        key=slug,
        subject=(subject or "").strip()[:200],
        statement=(statement or "").strip(),
        kind=kind,
        confidence=confidence,
        direction=direction,
        value_numeric=value_numeric,
        value_unit=value_unit,
        period=period,
        effective_from=effective_from,
        effective_to=effective_to,
        holding=holding,
        property=property,
        lease=lease,
        category=category,
        source_type=source_type,
        source_conversation=source_conversation,
        source_document=source_document,
        supersedes=previous,
        created_by_role=created_by_role,
        contains_personal_data=personal_data_present(landlord, statement),
    )
    reconcile(fact)
    fact.save()
    return fact


def retract(landlord, key: str, *, reason: str = "") -> TreasurerFact | None:
    slug = normalise_key(key)
    fact = TreasurerFact.objects.filter(
        landlord=landlord, key=slug, status=TreasurerFact.Status.ACTIVE
    ).first()
    if fact is None:
        return None
    fact.status = TreasurerFact.Status.RETRACTED
    fact.save(update_fields=["status"])
    return fact


# -------------------------------------------------------------------- reads
def active_facts(landlord, *, holding=None, on_date=None):
    """Facts usable in an analysis right now."""
    now = timezone.now()
    qs = TreasurerFact.objects.filter(
        landlord=landlord, status=TreasurerFact.Status.ACTIVE
    ).exclude(expires_at__lt=now)
    if holding is not None:
        from django.db.models import Q

        qs = qs.filter(Q(holding=holding) | Q(holding__isnull=True))
    if on_date is not None:
        from django.db.models import Q

        qs = qs.filter(
            Q(effective_from__isnull=True) | Q(effective_from__lte=on_date)
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
    return qs


def render_for_pack(landlord, *, holding=None, on_date=None) -> dict:
    """The fact block a deliberation reasons over.

    Split into two lists on purpose. `usable` may be added to totals;
    `shown_not_counted` may not, because the ledger already holds most of it.
    Presenting them as one list would be how a correction turns into a
    double-count, so the separation is structural rather than a caption.
    """
    usable, flagged = [], []
    for fact in active_facts(landlord, holding=holding, on_date=on_date):
        row = {
            "id": str(fact.pk),
            "subject": fact.subject,
            "statement": fact.statement,
            "amount": str(fact.value_numeric) if fact.value_numeric is not None else None,
            "unit": fact.value_unit,
            "period": fact.period,
            "direction": fact.direction,
            "from": fact.effective_from.isoformat() if fact.effective_from else None,
            "to": fact.effective_to.isoformat() if fact.effective_to else None,
            "source": fact.source_type,
            "confidence": fact.confidence,
        }
        if fact.double_count_risk:
            row["already_in_books"] = str(fact.ledger_overlap_amount or "0.00")
            row["why_not_counted"] = (
                "Your books already record most of this for the same period and "
                "property, so counting it again would overstate the figure. "
                "Shown so you can tell me if the ledger entry is wrong instead."
            )
            flagged.append(row)
        else:
            usable.append(row)

    return {
        "usable": usable,
        "shown_not_counted": flagged,
        "instruction": (
            "Facts under `usable` may be included in totals. Facts under "
            "`shown_not_counted` must NOT be added to any total — mention them "
            "and say why if they matter to the answer."
        )
        if flagged
        else "",
    }


# -------------------------------------------------- parsing a landlord's words
_MONEY = re.compile(r"\$?\s*([\d,]+(?:\.\d{1,2})?)")
_PER_MONTH = re.compile(r"\b(?:/\s*mo|per month|a month|monthly|/month)\b", re.I)
_PER_YEAR = re.compile(r"\b(?:/\s*yr|per year|a year|annually|/year)\b", re.I)
_FOR_A_YEAR = re.compile(r"\bfor (?:a|one|1) year\b", re.I)


def parse_amount(text: str):
    """(Decimal|None, period). Deliberately conservative — an ambiguous figure
    should produce a question, not a guess."""
    match = _MONEY.search(text or "")
    if not match:
        return None, TreasurerFact.Period.ONE_TIME
    try:
        amount = Decimal(match.group(1).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None, TreasurerFact.Period.ONE_TIME
    if _PER_MONTH.search(text):
        return amount, TreasurerFact.Period.MONTHLY
    if _PER_YEAR.search(text):
        return amount, TreasurerFact.Period.ANNUAL
    return amount, TreasurerFact.Period.ONE_TIME


def infer_direction(text: str) -> str:
    lowered = (text or "").casefold()
    if any(
        w in lowered
        for w in ("rent", "took", "received", "income", "paid us", "collected")
    ):
        return TreasurerFact.Direction.INCOME
    if any(
        w in lowered for w in ("spent", "cost", "paid for", "expense", "bill", "fee")
    ):
        return TreasurerFact.Direction.EXPENSE
    return TreasurerFact.Direction.NEUTRAL
