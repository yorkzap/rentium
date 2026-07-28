"""Tool implementation for recording a financial fact the ledger lacks."""

from __future__ import annotations

from datetime import date

from .domain_crud import _confirmed, _preview, _resolve_holding
from .models import TreasurerFact


def _day(raw, field):
    text = str(raw or "").strip()[:10]
    if not text:
        return None, None
    try:
        return date.fromisoformat(text), None
    except ValueError:
        return None, {"error": f"{field} must be YYYY-MM-DD, got {raw!r}."}


def record_treasurer_fact(
    landlord,
    *,
    subject: str,
    fact: str,
    amount: str = "",
    period: str = "",
    direction: str = "",
    holding_name: str = "",
    effective_from: str = "",
    effective_to: str = "",
    confirm: str = "",
) -> dict:
    from . import treasurer_facts as facts

    subject = (subject or "").strip()
    statement = (fact or "").strip()
    if not subject:
        return {"error": "subject is required — what is this fact about?"}

    parsed_amount, parsed_period = facts.parse_amount(amount or statement)
    if (amount or "").strip() and parsed_amount is None:
        return {"error": f"I couldn't read an amount from {amount!r}."}
    chosen_period = (period or "").strip().upper() or parsed_period
    if chosen_period not in TreasurerFact.Period.values:
        return {
            "error": f"period must be one of {sorted(TreasurerFact.Period.values)}."
        }

    holding = None
    if (holding_name or "").strip():
        holding, err = _resolve_holding(landlord, holding_name)
        if err:
            return {"error": err}

    refusal = facts.rejects(statement, value_numeric=parsed_amount)
    if refusal:
        return {"refused": True, "error": refusal}

    start, err = _day(effective_from, "effective_from")
    if err:
        return err
    end, err = _day(effective_to, "effective_to")
    if err:
        return err
    if parsed_amount is not None and chosen_period != TreasurerFact.Period.ONE_TIME:
        if not start or not end:
            return {
                "error": (
                    "A per-month or per-year figure needs the period it covers "
                    "(effective_from and effective_to), or I can't tell how much "
                    "it adds up to."
                )
            }

    chosen_direction = (
        (direction or "").strip().upper() or facts.infer_direction(statement)
    )
    if chosen_direction not in TreasurerFact.Direction.values:
        return {
            "error": (
                f"direction must be one of {sorted(TreasurerFact.Direction.values)}."
            )
        }

    key = facts.normalise_key(subject)
    existing = TreasurerFact.objects.filter(
        landlord=landlord, key=key, status=TreasurerFact.Status.ACTIVE
    ).first()

    # Reconcile on an UNSAVED instance so the preview can warn about a
    # double-count before the landlord confirms, not after.
    probe = TreasurerFact(
        landlord=landlord,
        value_numeric=parsed_amount,
        period=chosen_period,
        direction=chosen_direction,
        effective_from=start,
        effective_to=end,
        holding=holding,
    )
    check = facts.reconcile(probe)

    preview = {
        "subject": key,
        "fact": statement,
        "amount": str(parsed_amount) if parsed_amount is not None else "",
        "period": chosen_period,
        "direction": chosen_direction,
        "applies_to": holding.name if holding else "whole portfolio",
        "covers": f"{start} to {end}" if start and end else "",
        "replaces": existing.statement if existing else "",
    }
    if check.get("risk"):
        preview["already_in_books"] = str(check.get("overlap"))
        preview["double_count_warning"] = (
            f"Your books already record ${check.get('overlap')} for that "
            f"property and period. I'll keep this on file but leave it OUT of "
            f"totals, so it can't be counted twice. If the ledger is the thing "
            f"that's wrong, correct that instead."
        )

    if not _confirmed(confirm):
        return _preview(
            "record_treasurer_fact",
            preview,
            "Replaces what I had on this."
            if existing
            else "Records this for future financial analysis.",
        )

    row = facts.write(
        landlord,
        key=key,
        subject=subject,
        statement=statement,
        kind=TreasurerFact.Kind.LANDLORD_ASSERTED,
        confidence=TreasurerFact.Confidence.STATED,
        direction=chosen_direction,
        value_numeric=parsed_amount,
        period=chosen_period,
        effective_from=start,
        effective_to=end,
        holding=holding,
    )
    return {
        "recorded": True,
        "subject": row.key,
        "fact": row.statement,
        "replaced": preview["replaces"],
        "counted_in_totals": not row.double_count_risk,
        "already_in_books": str(row.ledger_overlap_amount or "0.00"),
    }
