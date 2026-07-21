"""
Historical import: CSV → staged rows the landlord can freely edit → commit
posts each clean row through the SAME ledger/services writers everything
else uses, inside one atomic transaction per batch. Staged rows are the
only mutable ledger-adjacent state in the app — by design, so a landlord
can fix a mis-parsed date or wrong property before anything becomes a
permanent, append-only fact.

Scope (v1): CSV only. XLSX would need a new dependency (openpyxl) — a
deliberate cut, not an oversight; bank/Excel exports as CSV cover the
common case and this keeps the import path dependency-free.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from . import services as ledger_services
from .models import (
    CHARGE_TYPES,
    EntryType,
    ExpenseCategory,
    ImportBatch,
    PaymentMethod,
    StagedLedgerEntry,
)
from .services import LedgerError

# Known target fields a CSV column can map to.
TARGET_FIELDS = (
    "entry_type", "amount", "due_date", "effective_date", "paid_on",
    "property_name", "tenant_name", "category", "vendor", "description",
    "payment_method",
)

# Header text -> target field, for the auto-mapping guess. Landlord confirms
# or corrects before anything is staged.
_HEADER_GUESSES = {
    "type": "entry_type", "entry type": "entry_type", "kind": "entry_type",
    "amount": "amount", "amt": "amount", "value": "amount",
    "due date": "due_date", "due": "due_date",
    "date": "effective_date", "effective date": "effective_date",
    "paid on": "paid_on", "cleared": "paid_on", "bank date": "paid_on",
    "property": "property_name", "listing": "property_name", "unit": "property_name",
    "tenant": "tenant_name", "payer": "tenant_name", "who": "tenant_name",
    "category": "category",
    "vendor": "vendor", "payee": "vendor",
    "description": "description", "memo": "description", "notes": "description",
    "method": "payment_method", "payment method": "payment_method",
}


def guess_column_map(headers: list[str]) -> dict[str, str]:
    """Best-effort header -> target field guess. Never authoritative — the
    landlord reviews and corrects this before any row is staged."""
    guess = {}
    for h in headers:
        key = h.strip().lower()
        if key in _HEADER_GUESSES:
            guess[h] = _HEADER_GUESSES[key]
    return guess


def read_csv_rows(file_bytes: bytes) -> tuple[list[str], list[dict]]:
    """(headers, rows) from a CSV file's raw bytes. Rows are dicts keyed by
    the ORIGINAL header text (not yet mapped to target fields)."""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = [dict(r) for r in reader]
    return headers, rows


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(raw: str) -> Decimal | None:
    raw = (raw or "").strip().replace("$", "").replace(",", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    if not raw:
        return None
    try:
        return abs(Decimal(raw))  # ledger amounts are always positive
    except InvalidOperation:
        return None


def _guess_entry_type(raw: str) -> str:
    raw = (raw or "").strip().lower()
    table = {
        "rent": EntryType.RENT_CHARGE, "rent charge": EntryType.RENT_CHARGE,
        "utility": EntryType.UTILITY_CHARGE, "utility charge": EntryType.UTILITY_CHARGE,
        "deposit": EntryType.DEPOSIT_CHARGE, "deposit charge": EntryType.DEPOSIT_CHARGE,
        "fee": EntryType.FEE_CHARGE, "fee charge": EntryType.FEE_CHARGE,
        "charge": EntryType.OTHER_CHARGE, "other charge": EntryType.OTHER_CHARGE,
        "payment": EntryType.PAYMENT, "paid": EntryType.PAYMENT,
        "expense": EntryType.EXPENSE, "expense out": EntryType.EXPENSE,
        "deposit return": EntryType.DEPOSIT_RETURN, "deposit returned": EntryType.DEPOSIT_RETURN,
    }
    return table.get(raw, "")


def stage_rows(batch: ImportBatch, headers: list[str], raw_rows: list[dict],
                column_map: dict[str, str]) -> list[StagedLedgerEntry]:
    """Replace this batch's staged rows with fresh ones built from
    raw_rows + column_map. Safe to call again after the landlord fixes the
    mapping — previous staged edits are intentionally discarded (they were
    built from the old mapping)."""
    from rentium.leases.models import Lease
    from rentium.properties.models import Property

    StagedLedgerEntry.objects.filter(batch=batch).delete()

    inverse = {target: src for src, target in column_map.items() if target}
    props_by_name = {
        p.name.strip().lower(): p
        for p in Property.objects.filter(landlord=batch.landlord)
    }

    rows_out: list[StagedLedgerEntry] = []
    for i, raw in enumerate(raw_rows, start=1):

        def field(name):
            src = inverse.get(name)
            return (raw.get(src) or "").strip() if src else ""

        entry_type = _guess_entry_type(field("entry_type"))
        property_name = field("property_name")
        prop = props_by_name.get(property_name.strip().lower()) if property_name else None
        lease = None
        if prop is not None:
            lease = (
                Lease.objects.filter(landlord=batch.landlord, property=prop)
                .order_by("-created_at")
                .first()
            )

        row = StagedLedgerEntry(
            batch=batch,
            row_number=i,
            entry_type=entry_type,
            amount=_parse_amount(field("amount")),
            # A single generic "Date" column is common in bank/Excel exports
            # — it maps to effective_date, but a charge needs a due_date too,
            # so each falls back to the other when only one was given.
            due_date=_parse_date(field("due_date")) or _parse_date(field("effective_date")),
            effective_date=_parse_date(field("effective_date")) or _parse_date(field("due_date")),
            paid_on=_parse_date(field("paid_on")),
            property=prop,
            lease=lease,
            category=field("category"),
            vendor=field("vendor"),
            description=field("description") or property_name,
            payment_method=field("payment_method"),
            raw=raw,
        )
        row.issues = validate_row(row)
        rows_out.append(row)
    return StagedLedgerEntry.objects.bulk_create(rows_out)


def validate_row(row: StagedLedgerEntry) -> list[dict]:
    """Deterministic checks — every reason a row can't commit yet, in one
    place. Called on stage AND on every edit, so the row always shows why
    it's blocked."""
    issues: list[dict] = []

    valid_types = {c for c, _ in EntryType.choices}
    if row.entry_type not in valid_types:
        issues.append({"field": "entry_type", "message": "Missing or unrecognized type."})
    try:
        amount_ok = row.amount is not None and Decimal(row.amount) > 0
    except (InvalidOperation, TypeError):
        amount_ok = False
    if not amount_ok:
        issues.append({"field": "amount", "message": "Amount must be a positive number."})

    if row.entry_type in CHARGE_TYPES:
        if not row.due_date:
            issues.append({"field": "due_date", "message": "Charges need a due date."})
        if not row.property_id and not row.lease_id:
            issues.append({"field": "property", "message": "No matching property found."})
    elif row.entry_type == EntryType.EXPENSE:
        if not row.effective_date:
            issues.append({"field": "effective_date", "message": "Expenses need a date."})
        valid_categories = {c for c, _ in ExpenseCategory.choices}
        if row.category and row.category.upper() not in valid_categories:
            issues.append(
                {"field": "category", "message": f"Unknown expense category {row.category!r}."}
            )
        if not row.description:
            issues.append({"field": "description", "message": "Expenses need a description."})
    elif row.entry_type in (EntryType.PAYMENT, EntryType.CREDIT):
        if not row.effective_date:
            issues.append({"field": "effective_date", "message": "Payments need a date."})
        if not row.settles_row_id:
            issues.append(
                {"field": "settles_row", "message": "Pick which charge row this settles."}
            )
        elif row.settles_row.entry_type not in CHARGE_TYPES:
            issues.append(
                {"field": "settles_row", "message": "That row isn't a charge — can't be settled."}
            )
        if row.payment_method and row.payment_method.upper() not in {
            c for c, _ in PaymentMethod.choices
        }:
            issues.append(
                {"field": "payment_method", "message": f"Unknown method {row.payment_method!r}."}
            )
    elif row.entry_type == EntryType.DEPOSIT_RETURN:
        # ledger.services.post_deposit_return always dates the entry "today"
        # (no effective_date param) — importing one under a chosen historical
        # date would silently misdate it, so this is unsupported for now
        # rather than quietly wrong. Record it as a plain EXPENSE-style note
        # or add the date param to post_deposit_return in a follow-up.
        issues.append(
            {
                "field": "entry_type",
                "message": "Deposit-return import isn't supported yet — record it "
                "directly in Financial instead.",
            }
        )

    # Duplicate-of-existing-ledger heuristic — a soft flag, not a hard block
    # elsewhere, but v1 keeps issue handling simple: any issue blocks commit.
    if row.amount and (row.effective_date or row.due_date) and row.entry_type in valid_types:
        from .models import LedgerEntry

        dupe = LedgerEntry.objects.not_voided().filter(
            landlord=row.batch.landlord, entry_type=row.entry_type, amount=row.amount,
            effective_date=row.effective_date or row.due_date,
        )
        if row.property_id:
            dupe = dupe.filter(property_id=row.property_id)
        if dupe.exists():
            issues.append(
                {
                    "field": "amount",
                    "message": "Possible duplicate of an existing ledger entry — review before committing.",
                }
            )
    return issues


@transaction.atomic
def commit_batch(batch: ImportBatch, *, created_by=None) -> dict:
    """Post every issue-free row through the normal ledger writers.
    Charges commit first (payments need a real charge to settle), then
    payments/credits, then expenses/deposit-returns. Idempotent: a row
    already committed (committed_entry set) is skipped, and every write
    carries idempotency_key=f"import:{row.id}" so a retried commit can
    never double-post even across separate calls."""
    if batch.status == ImportBatch.Status.DISCARDED:
        raise LedgerError("This batch was discarded.")

    rows = list(
        StagedLedgerEntry.objects.filter(batch=batch)
        .select_related("settles_row", "batch")
        .order_by("row_number")
    )
    clean = [r for r in rows if not r.issues and r.committed_entry_id is None]
    charges = [r for r in clean if r.entry_type in CHARGE_TYPES]
    settlements = [r for r in clean if r.entry_type in (EntryType.PAYMENT, EntryType.CREDIT)]
    # DEPOSIT_RETURN is excluded — see validate_row's note (would misdate).
    money_out = [r for r in clean if r.entry_type == EntryType.EXPENSE]

    committed, errors = 0, []
    # select_related made a SEPARATE in-memory copy of each row's
    # settles_row — updating charges[i].committed_entry does not propagate
    # to that cached copy. Track committed charges by row id instead of
    # relying on the (now stale) related-object reference. Seeded from ALL
    # rows (not just this call's `charges`) so a settlement whose charge
    # committed in an earlier partial commit still resolves.
    committed_charge_by_row: dict = {
        r.id: r.committed_entry for r in rows if r.committed_entry_id
    }

    for row in charges:
        try:
            entry, _ = ledger_services.post_charge(
                landlord=batch.landlord, tenant=row.tenant, lease=row.lease,
                property=row.property, amount=row.amount, due_date=row.due_date,
                entry_type=row.entry_type, description=row.description or "Imported charge",
                idempotency_key=f"import:{row.id}", created_by=created_by,
                metadata={"import_batch": str(batch.pk)},
            )
            row.committed_entry = entry
            row.save(update_fields=["committed_entry"])
            committed_charge_by_row[row.id] = entry
            committed += 1
        except Exception as exc:  # noqa: BLE001 — report, don't abort the batch
            errors.append({"row": row.row_number, "error": str(exc)})

    for row in settlements:
        charge_entry = (
            committed_charge_by_row.get(row.settles_row_id) if row.settles_row_id else None
        )
        if charge_entry is None:
            errors.append(
                {"row": row.row_number, "error": "Its charge row didn't commit — fix that first."}
            )
            continue
        try:
            if row.entry_type == EntryType.PAYMENT:
                entry, _ = ledger_services.record_payment(
                    charge=charge_entry, amount=row.amount,
                    payment_method=row.payment_method or PaymentMethod.OTHER,
                    payment_date=row.effective_date, paid_by=row.tenant,
                    idempotency_key=f"import:{row.id}", created_by=created_by,
                    notes="Imported historical payment",
                )
            else:  # CREDIT
                entry, _ = ledger_services.post_credit(
                    charge=charge_entry, amount=row.amount,
                    reason=row.description or "Imported credit",
                    idempotency_key=f"import:{row.id}", created_by=created_by,
                )
            row.committed_entry = entry
            row.save(update_fields=["committed_entry"])
            committed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"row": row.row_number, "error": str(exc)})

    for row in money_out:
        try:
            entry, _ = ledger_services.post_expense(
                landlord=batch.landlord, property=row.property, amount=row.amount,
                category=row.category or ExpenseCategory.OTHER,
                description=row.description or "Imported expense",
                incurred_date=row.effective_date, vendor=row.vendor,
                idempotency_key=f"import:{row.id}", created_by=created_by,
                paid_on=row.paid_on,
            )
            row.committed_entry = entry
            row.save(update_fields=["committed_entry"])
            committed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"row": row.row_number, "error": str(exc)})

    remaining = StagedLedgerEntry.objects.filter(
        batch=batch, committed_entry__isnull=True
    ).exists()
    if not remaining:
        batch.status = ImportBatch.Status.COMMITTED
        batch.committed_at = timezone_now()
        batch.save(update_fields=["status", "committed_at"])

    return {
        "committed": committed,
        "errors": errors,
        "blocked_by_issues": len(rows) - len(clean),
        "batch_status": batch.status,
    }


def timezone_now():
    from django.utils import timezone

    return timezone.now()
