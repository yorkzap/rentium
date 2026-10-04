"""RAMA adapters to the same financial workflows used by the dashboard."""

import json
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .domain_crud import _confirmed, _preview
from rentium.ledger.services import LedgerError


def money_overview(
    landlord,
    month: str = "",
    holding_id: str = "",
    unit_id: str = "",
    property_id: str = "",
) -> dict:
    """Exact recorded money, overdue rent, unpaid bills, and supporting entries.
    Use this for cash movement and income questions. Never call it bank balance.
    month is YYYY-MM; optional scope IDs must come from current records.
    """
    from rentium.ledger.money import overview

    try:
        start = (
            date.fromisoformat(month + "-01")
            if month
            else timezone.localdate().replace(day=1)
        )
        end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
        return overview(
            landlord,
            start,
            end,
            holding_id=holding_id or None,
            unit_id=unit_id or None,
            property_id=property_id or None,
        )
    except (LedgerError, ValueError, ValidationError) as exc:
        return {"error": str(exc)}


@transaction.atomic
def review_bank_statements(landlord, batch_id: str = "") -> dict:
    """List saved bank statement reviews, or read exact rows, decisions and issues.
    Drafts persist across conversations. Explicitly select a batch; never guess
    the newest one. Ledger candidates carry exact IDs for allocation decisions.
    """
    from rentium.ledger.models import BankStatement
    from rentium.ledger import reconciliation as service

    if not batch_id:
        return {
            "statements": [
                {
                    "batch_id": str(s.batch_id),
                    "account": s.account,
                    "filename": s.batch.source_filename,
                    "status": s.batch.status,
                    "remaining": s.transactions.filter(result={}).count(),
                }
                for s in BankStatement.objects.filter(batch__landlord=landlord)
                .select_related("batch")
                .order_by("-batch__created_at")
            ]
        }
    try:
        statement = BankStatement.objects.select_related("batch").get(
            batch_id=batch_id, batch__landlord=landlord
        )
        return {
            **service.detail(statement),
            "ledger_candidates": service.candidates(statement),
        }
    except (
        BankStatement.DoesNotExist,
        LedgerError,
        ValueError,
        ValidationError,
    ) as exc:
        return {"error": str(exc)}


@transaction.atomic
def reconcile_bank_statement(
    landlord,
    batch_id: str,
    row_ids: str = "",
    decisions_json: str = "",
    preview_id: str = "",
    confirm: str = "",
) -> dict:
    """Prepare one complete bank-review preview; explicit confirmation posts it.
    row_ids: comma-separated source row IDs from review_bank_statements.
    decisions_json optionally revises drafts as [{row_id, decision}]. Decisions:
    payment with allocations [{entry_id, amount}]; pay_expense/link with entry_id;
    expense with category, incurred_date, property_id/holding_id or portfolio_wide;
    exclude with reason transfer/personal/duplicate/not_a_transaction.
    document_id attaches a receipt. Never invent IDs, dates or accounting facts.
    Unallocated money stays in review. confirm is injected only by the plan runner.
    """
    from rentium.ledger import reconciliation as service

    try:
        if _confirmed(confirm):
            return service.commit(landlord, batch_id, preview_id, actor=landlord.user)
        ids = [int(value.strip()) for value in row_ids.split(",") if value.strip()]
        changes = json.loads(decisions_json) if decisions_json else []
        if not isinstance(changes, list) or len(changes) > 5000:
            raise ValueError("Provide a list of draft row decisions.")
        for change in changes:
            service.decide(landlord, batch_id, change["row_id"], change["decision"])
        preview = service.preview(landlord, batch_id, ids)
        result = _preview(
            "reconcile_bank_statement",
            preview,
            "One approval commits these exact effects atomically. Any remaining rows stay in review.",
        )
        result["resolved_arguments"] = {
            "batch_id": batch_id,
            "row_ids": row_ids,
            "decisions_json": "",
            "preview_id": preview["id"],
        }
        return result
    except (LedgerError, ValueError, TypeError, KeyError, ValidationError) as exc:
        return {"error": str(exc)}
