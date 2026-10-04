"""Statement review shared by REST and RAMA. Only commit() posts money."""

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, When, Value, IntegerField, Q
from django.utils import timezone

from . import services
from .models import (
    BankStatement,
    BankTransaction,
    CHARGE_TYPES,
    EntryType,
    ExpenseCategory,
    ImportBatch,
    LedgerEntry,
)
from .money import entry_row, money
from .statement_files import (
    FIELDS,
    DATE_FORMATS,
    decimal_amount,
    guess_mapping,
    parse_row,
    read_statement,
)

Error = services.LedgerError


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def locked_statement(landlord, batch_id):
    # One owner lock also serializes overlapping uploads/commits in different
    # batches, so file-level retries cannot escape the duplicate check.
    type(landlord).objects.select_for_update().get(pk=landlord.pk)
    try:
        return (
            BankStatement.objects.select_for_update()
            .select_related("batch")
            .get(batch_id=batch_id, batch__landlord=landlord)
        )
    except BankStatement.DoesNotExist as exc:
        raise Error("Statement not found in this portfolio.") from exc


def editable(statement):
    if statement.batch.status != ImportBatch.Status.DRAFT:
        raise Error("This statement is already closed.")
    if statement.staging.get("state") == "QUEUED":
        raise Error(
            "Statement preparation is still running. Your source file is saved."
        )


def invalidate(statement):
    statement.revision += 1
    statement.preview = {}
    statement.save(update_fields=["revision", "preview"])


@transaction.atomic
def upload(landlord, content, filename, account, *, actor=None, worksheet=""):
    account = str(account).strip()
    if not account or len(account) > 120:
        raise Error("Name the bank account (at most 120 characters).")
    headers, rows, sheets, selected = read_statement(content, filename, worksheet)
    type(landlord).objects.select_for_update().get(pk=landlord.pk)
    file_hash = hashlib.sha256(content).hexdigest()
    old = BankStatement.objects.filter(
        batch__landlord=landlord,
        account=account,
        source_hash=file_hash,
        worksheet=selected,
    ).first()
    if old:
        return detail(old), False
    previous = (
        BankStatement.objects.filter(batch__landlord=landlord, account=account)
        .select_related("batch")
        .order_by("-pk")
        .first()
    )
    mapping = (
        previous.batch.column_map
        if previous and set(previous.batch.column_map).issubset(headers)
        else guess_mapping(headers)
    )
    batch = ImportBatch.objects.create(
        landlord=landlord,
        label=f"{account} — {filename}"[:200],
        source_filename=filename[:255],
        created_by=actor,
        column_map=mapping,
    )
    batch.source_file.save(filename, ContentFile(content), save=True)
    statement = BankStatement.objects.create(
        batch=batch,
        account=account,
        source_hash=file_hash,
        worksheet=selected,
        date_format=previous.date_format if previous else "%Y-%m-%d",
    )
    return {
        **detail(statement),
        "headers": headers,
        "worksheets": sheets,
        "mapping": mapping,
    }, True


@transaction.atomic
def map_statement(landlord, batch_id, mapping, date_format, worksheet=""):
    statement = locked_statement(landlord, batch_id)
    editable(statement)
    if statement.transactions.exclude(result={}).exists():
        raise Error(
            "Rows have been approved. Start a new statement to change its mapping."
        )
    if date_format not in DATE_FORMATS or not isinstance(mapping, dict):
        raise Error("Choose a supported mapping and date format.")
    with statement.batch.source_file.open("rb") as source:
        headers, rows, _, selected = read_statement(
            source.read(),
            statement.batch.source_filename,
            worksheet or statement.worksheet,
        )
    targets = [value for value in mapping.values() if value]
    if (
        any(k not in headers for k in mapping)
        or any(v not in FIELDS for v in targets)
        or len(targets) != len(set(targets))
    ):
        raise Error("Map each field once using the columns in this statement.")
    if "date" not in targets or not (
        "amount" in targets or {"debit", "credit"}.intersection(targets)
    ):
        raise Error("Map a date and either a signed amount or debit/credit columns.")
    if "amount" in targets and {"debit", "credit"}.intersection(targets):
        raise Error("Choose signed amount OR debit/credit columns.")
    statement.transactions.all().delete()  # Unposted staging only; source file retained.
    created = []
    for number, raw in enumerate(rows, 2):
        item = BankTransaction(statement=statement, row_number=number, raw=raw)
        try:
            booked, amount, description, reference = parse_row(
                raw, mapping, date_format
            )
            if booked > timezone.localdate():
                raise ValueError("Future bank dates require a corrected export.")
            item.booked_on, item.signed_amount = booked, amount
            item.description, item.reference = description[:1000], reference[:255]
            item.fingerprint = digest(
                [
                    statement.account,
                    str(booked),
                    money(amount),
                    description.casefold(),
                    reference,
                ]
            )
        except ValueError as exc:
            item.parse_error = str(exc)
        created.append(item)
    BankTransaction.objects.bulk_create(created)
    statement.worksheet, statement.date_format = selected, date_format
    statement.save(update_fields=["worksheet", "date_format"])
    statement.batch.column_map = mapping
    statement.batch.save(update_fields=["column_map"])
    invalidate(statement)
    suggest(statement)
    return detail(statement)


@transaction.atomic
def prepare_statement(landlord, batch_id, mapping, date_format, worksheet=""):
    """Keep small reviews immediate; stage larger exports in the worker."""
    statement = locked_statement(landlord, batch_id)
    if statement.staging.get("state") == "QUEUED":
        queued = datetime.fromisoformat(statement.staging["queued_at"])
        if timezone.now() - queued < timedelta(minutes=5):
            return detail(statement)
        # A lost worker/broker delivery may be retried; old job IDs cannot run.
        statement.staging = {}
    editable(statement)
    if statement.transactions.exclude(result={}).exists():
        raise Error("Approved statements cannot be remapped.")
    with statement.batch.source_file.open("rb") as source:
        _, rows, _, _ = read_statement(
            source.read(),
            statement.batch.source_filename,
            worksheet or statement.worksheet,
        )
    if len(rows) <= 100:
        statement.staging = {}
        statement.save(update_fields=["staging"])
        return map_statement(landlord, batch_id, mapping, date_format, worksheet)
    if not isinstance(mapping, dict) or date_format not in DATE_FORMATS:
        raise Error("Choose a supported mapping and date format.")
    job_id = str(uuid.uuid4())
    statement.staging = {
        "state": "QUEUED",
        "job_id": job_id,
        "queued_at": timezone.now().isoformat(),
    }
    statement.save(update_fields=["staging"])
    invalidate(statement)

    def enqueue():
        from .tasks import prepare_bank_statement

        try:
            prepare_bank_statement.delay(
                str(batch_id), job_id, mapping, date_format, worksheet
            )
        except Exception:
            BankStatement.objects.filter(
                batch_id=batch_id, staging__job_id=job_id
            ).update(
                staging={
                    "state": "FAILED",
                    "message": "The preparation worker is unavailable. Your source is saved; retry preparation.",
                }
            )

    transaction.on_commit(enqueue)
    return detail(statement)


def candidates(statement):
    landlord = statement.batch.landlord
    entries = (
        LedgerEntry.objects.filter(landlord=landlord)
        .not_voided()
        .select_related("property", "holding")
        .with_settlement()
    )
    # A bounded selection is presented explicitly; full exact-ID validation
    # below never relies on the candidate list for ownership or authorization.
    return [
        entry_row(e)
        for e in entries.exclude(entry_type=EntryType.REVERSAL)
        .annotate(
            review_priority=Case(
                When(
                    Q(entry_type__in=list(CHARGE_TYPES), outstanding__gt=0)
                    | Q(entry_type=EntryType.EXPENSE, paid_on__isnull=True),
                    then=Value(0),
                ),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("review_priority", "-effective_date", "id")[:1000]
    ]


def duplicate_rows(row):
    if not row.fingerprint:
        return []
    return list(
        BankTransaction.objects.filter(
            statement__batch__landlord=row.statement.batch.landlord,
            fingerprint=row.fingerprint,
        )
        .exclude(pk=row.pk)
        .exclude(result={})
        .values_list("pk", flat=True)
    )


def suggest(statement):
    pool = candidates(statement)
    for row in statement.transactions.all():
        if row.parse_error or row.decision or duplicate_rows(row):
            continue
        tokens = set(re.findall(r"[a-z0-9]{4,}", row.description.lower())) - {
            "payment",
            "transfer",
            "etransfer",
            "deposit",
            "withdrawal",
        }
        matches = []
        for entry in pool:
            relevant = set(
                re.findall(
                    r"[a-z0-9]{4,}",
                    (entry["description"] + " " + entry["scope_name"]).lower(),
                )
            )
            if not tokens.intersection(relevant):
                continue
            amount = abs(row.signed_amount)
            if (
                row.signed_amount > 0
                and entry["entry_type"] in CHARGE_TYPES
                and Decimal(entry["outstanding"] or "0") == amount
            ):
                matches.append(
                    {
                        "action": "payment",
                        "allocations": [
                            {"entry_id": entry["id"], "amount": money(amount)}
                        ],
                    }
                )
            elif (
                row.signed_amount < 0
                and entry["entry_type"] == EntryType.EXPENSE
                and Decimal(entry["amount"]) == amount
                and not entry["paid_on"]
            ):
                matches.append({"action": "pay_expense", "entry_id": entry["id"]})
            elif (
                entry["entry_type"] in (EntryType.PAYMENT, EntryType.EXPENSE)
                and Decimal(entry["amount"]) == amount
                and (entry["paid_on"] or entry["effective_date"]) == str(row.booked_on)
                and (
                    (row.signed_amount > 0)
                    == (entry["entry_type"] == EntryType.PAYMENT)
                )
            ):
                matches.append({"action": "link", "entry_id": entry["id"]})
        if len(matches) == 1:
            row.decision = matches[0]
            row.save(update_fields=["decision"])


def resolve_entry(landlord, ident):
    try:
        return (
            LedgerEntry.objects.select_for_update(of=("self",))
            .filter(landlord=landlord)
            .not_voided()
            .get(pk=ident)
        )
    except (LedgerEntry.DoesNotExist, ValueError) as exc:
        raise Error(
            "The selected ledger record is missing, voided, or outside this portfolio."
        ) from exc


def validated(row):
    """Return canonical effects with the live state that approval freezes."""
    decision = row.decision
    if not isinstance(decision, dict):
        raise Error("A decision must be an object.")
    action = decision.get("action")
    if action == "exclude":
        reason = str(decision.get("reason", "")).strip()
        if reason not in {"transfer", "personal", "duplicate", "not_a_transaction"}:
            raise Error("Choose why this row is excluded.")
        return {"action": action, "reason": reason}
    if row.parse_error:
        raise Error(row.parse_error)
    if not action:
        raise Error("Choose how to record this transaction.")
    if duplicate_rows(row) and decision.get("separate_transaction") is not True:
        raise Error(
            "This bank row was already reviewed. Exclude it as duplicate or explicitly mark it a separate transaction."
        )
    landlord = row.statement.batch.landlord
    amount = abs(row.signed_amount)
    effect = {"action": action, "amount": money(amount), "date": str(row.booked_on)}
    if action == "payment":
        if row.signed_amount < 0:
            raise Error("A rent/deposit payment must be incoming money.")
        allocations = decision.get("allocations", [])
        if (
            not isinstance(allocations, list)
            or not allocations
            or len(allocations) > 100
        ):
            raise Error("Allocate the complete payment to one or more charges.")
        effect["allocations"] = []
        seen = set()
        for allocation in allocations:
            if not isinstance(allocation, dict):
                raise Error("Each allocation must contain a charge ID and an amount.")
            entry = resolve_entry(landlord, allocation.get("entry_id"))
            value = decimal_amount(allocation.get("amount", ""))
            outstanding = services.outstanding_on(entry)
            if (
                entry.entry_type not in CHARGE_TYPES
                or value <= 0
                or value > outstanding
                or entry.pk in seen
            ):
                raise Error(
                    "Each allocation needs a different open charge and cannot exceed its balance."
                )
            seen.add(entry.pk)
            effect["allocations"].append(
                {
                    "entry_id": str(entry.pk),
                    "amount": money(value),
                    "outstanding": money(outstanding),
                    "description": entry.description,
                }
            )
        if sum(Decimal(a["amount"]) for a in effect["allocations"]) != amount:
            raise Error(
                "Allocations must equal the bank amount. Unallocated money remains in review."
            )
    elif action in {"pay_expense", "link"}:
        entry = resolve_entry(landlord, decision.get("entry_id"))
        if entry.amount != amount:
            raise Error("The selected record must equal the bank transaction amount.")
        if action == "pay_expense":
            if (
                row.signed_amount > 0
                or entry.entry_type != EntryType.EXPENSE
                or entry.paid_on
            ):
                raise Error("Select an unpaid expense for outgoing money.")
        elif not (
            (
                row.signed_amount > 0
                and entry.entry_type == EntryType.PAYMENT
                and entry.effective_date == row.booked_on
            )
            or (
                row.signed_amount < 0
                and entry.entry_type == EntryType.EXPENSE
                and entry.paid_on == row.booked_on
            )
        ):
            raise Error(
                "Link only an already recorded payment/cleared expense with the same date and direction."
            )
        effect.update(
            entry_id=str(entry.pk),
            paid_on=str(entry.paid_on) if entry.paid_on else None,
            description=entry.description,
        )
        previous = (
            BankTransaction.objects.filter(statement__batch__landlord=landlord)
            .exclude(pk=row.pk)
            .exclude(result={})
        )
        if any(
            str(entry.pk) in other.result.get("entry_ids", []) for other in previous
        ):
            raise Error(
                "This ledger record already matches a reviewed bank row. Exclude a duplicate instead."
            )
    elif action == "expense":
        from rentium.properties.models import Property, PropertyHolding

        if row.signed_amount > 0:
            raise Error("An expense must be outgoing money.")
        category = decision.get("category")
        if category not in ExpenseCategory.values:
            raise Error("Choose an expense category.")
        prop = (
            Property.objects.filter(
                landlord=landlord, pk=decision.get("property_id")
            ).first()
            if decision.get("property_id")
            else None
        )
        holding = (
            PropertyHolding.objects.filter(
                landlord=landlord, pk=decision.get("holding_id")
            ).first()
            if decision.get("holding_id")
            else None
        )
        if (
            decision.get("property_id")
            and not prop
            or decision.get("holding_id")
            and not holding
        ):
            raise Error("Choose a scope in this portfolio.")
        if not prop and not holding and decision.get("portfolio_wide") is not True:
            raise Error(
                "Choose an address, listing, or explicitly portfolio-wide expense."
            )
        if prop and holding and prop.holding_id != holding.pk:
            raise Error("The listing and address do not agree.")
        if (prop or holding) and decision.get("portfolio_wide") is True:
            raise Error("Choose either a property scope or portfolio-wide, not both.")
        incurred = date.fromisoformat(
            decision.get("incurred_date") or str(row.booked_on)
        )
        if incurred > timezone.localdate():
            raise Error("An incurred date cannot be in the future.")
        effect.update(
            category=category,
            property_id=str(prop.pk) if prop else None,
            holding_id=str(holding.pk) if holding else None,
            incurred_date=str(incurred),
            description=str(decision.get("description") or row.description)[:255],
            vendor=str(decision.get("vendor") or "")[:150],
        )
        if not effect["description"]:
            raise Error("Describe this expense.")
        duplicates = services.find_duplicate_expense_candidates(
            landlord, amount=amount, on_date=incurred, property=prop, holding=holding
        )
        if duplicates and decision.get("separate_transaction") is not True:
            raise Error(
                "A similar expense already exists. Link it, or explicitly mark this as a separate cost."
            )
    else:
        raise Error("Unsupported transaction decision.")
    if decision.get("document_id"):
        from rentium.rama.models import RamaDocument

        doc = (
            RamaDocument.objects.select_for_update()
            .filter(
                landlord=landlord, pk=decision["document_id"], deleted_at__isnull=True
            )
            .first()
        )
        if (
            not doc
            or action not in {"expense", "pay_expense", "link"}
            or row.signed_amount > 0
        ):
            raise Error("Select a receipt in this portfolio for an expense.")
        if doc.amount is None or doc.amount != amount:
            raise Error(
                "Review the receipt amount in Documents before linking it to this bank transaction."
            )
        if doc.status not in {
            RamaDocument.Status.NEEDS_REVIEW,
            RamaDocument.Status.READY,
            RamaDocument.Status.FILED,
        }:
            raise Error(
                "Wait for receipt processing and review its extracted facts in Documents."
            )
        if doc.ledger_entry_id and str(doc.ledger_entry_id) != effect.get("entry_id"):
            raise Error(
                "This receipt already belongs to another expense. Select that expense instead."
            )
        if effect.get("entry_id"):
            existing = getattr(entry, "source_document", None)
            if existing and existing.pk != doc.pk:
                raise Error(
                    "This expense already has a receipt. Keep that evidence and review the new document separately."
                )
        effect["document"] = {
            "id": str(doc.pk),
            "amount": money(doc.amount),
            "entry_id": str(doc.ledger_entry_id) if doc.ledger_entry_id else None,
        }
    effect["separate_transaction"] = decision.get("separate_transaction") is True
    return effect


@transaction.atomic
def decide(landlord, batch_id, row_id, decision):
    statement = locked_statement(landlord, batch_id)
    editable(statement)
    row = statement.transactions.select_for_update().get(pk=row_id)
    if row.result:
        raise Error("This row has already been approved.")
    if not isinstance(decision, dict):
        raise Error("A decision must be an object.")
    row.decision = decision
    # Incomplete drafts are useful and persist; errors remain visible in detail.
    row.save(update_fields=["decision"])
    invalidate(statement)
    return detail(statement)


def row_payload(row):
    issue = ""
    if not row.result:
        try:
            validated(row)
        except (Error, ValueError, TypeError, KeyError, ValidationError) as exc:
            issue = str(exc)
    return {
        "id": row.pk,
        "row_number": row.row_number,
        "date": str(row.booked_on) if row.booked_on else None,
        "amount": money(row.signed_amount) if row.signed_amount is not None else None,
        "description": row.description,
        "reference": row.reference,
        "raw": row.raw,
        "decision": row.decision,
        "result": row.result,
        "issue": issue,
    }


@transaction.atomic
def detail(statement):
    rows = [
        row_payload(r)
        for r in statement.transactions.select_related("statement__batch__landlord")
    ]
    with statement.batch.source_file.open("rb") as source:
        headers, _, sheets, _ = read_statement(
            source.read(), statement.batch.source_filename, statement.worksheet
        )
    return {
        "id": str(statement.batch_id),
        "account": statement.account,
        "currency": statement.currency,
        "filename": statement.batch.source_filename,
        "status": statement.batch.status,
        "revision": statement.revision,
        "mapping": statement.batch.column_map,
        "date_format": statement.date_format,
        "worksheet": statement.worksheet,
        "worksheets": sheets,
        "headers": headers,
        "rows": rows,
        "preview": statement.preview or None,
        "receipt": statement.receipt or None,
        "staging": statement.staging,
    }


def build_preview(statement, row_ids):
    rows = list(
        statement.transactions.filter(pk__in=row_ids)
        .select_related("statement__batch__landlord")
        .order_by("row_number")
    )
    if not rows or len(rows) != len(set(row_ids)) or any(r.result for r in rows):
        raise Error("Select unapproved rows from this statement.")
    effects, used, charges, expense_keys, documents, new_expenses = (
        [],
        set(),
        {},
        set(),
        set(),
        set(),
    )
    for row in rows:
        effect = validated(row)
        if effect["action"] != "exclude":
            if row.fingerprint in used and not effect["separate_transaction"]:
                raise Error(
                    "Repeated bank rows need an explicit separate-transaction decision."
                )
            used.add(row.fingerprint)
        for a in effect.get("allocations", []):
            charges[a["entry_id"]] = charges.get(a["entry_id"], Decimal("0")) + Decimal(
                a["amount"]
            )
            if charges[a["entry_id"]] > Decimal(a["outstanding"]):
                raise Error(
                    "The selected rows together exceed a charge's outstanding balance."
                )
        if effect.get("entry_id"):
            if effect["entry_id"] in expense_keys:
                raise Error(
                    "A recorded payment or expense can only match one selected bank row."
                )
            expense_keys.add(effect["entry_id"])
        if effect.get("document"):
            did = effect["document"]["id"]
            if did in documents:
                raise Error("A receipt can only support one selected transaction.")
            documents.add(did)
        if effect["action"] == "expense":
            key = (
                effect["amount"],
                effect["incurred_date"],
                effect["property_id"],
                effect["holding_id"],
            )
            if key in new_expenses and not effect["separate_transaction"]:
                raise Error(
                    "Similar new expenses in this batch need an explicit separate-cost decision."
                )
            new_expenses.add(key)
        effects.append(
            {
                "row_id": row.pk,
                "source": {
                    "date": str(row.booked_on),
                    "amount": money(row.signed_amount) if row.signed_amount else None,
                    "description": row.description,
                },
                "effect": effect,
            }
        )
    return {
        "batch_id": str(statement.batch_id),
        "revision": statement.revision,
        "effects": effects,
        "remaining": statement.transactions.filter(result={})
        .exclude(pk__in=row_ids)
        .count(),
        "incoming": money(
            sum(
                (
                    r.signed_amount
                    for r in rows
                    if r.signed_amount and r.signed_amount > 0
                ),
                Decimal("0"),
            )
        ),
        "outgoing": money(
            sum(
                (
                    -r.signed_amount
                    for r in rows
                    if r.signed_amount and r.signed_amount < 0
                ),
                Decimal("0"),
            )
        ),
    }


@transaction.atomic
def preview(landlord, batch_id, row_ids):
    statement = locked_statement(landlord, batch_id)
    editable(statement)
    payload = build_preview(statement, row_ids)
    statement.preview = {
        **payload,
        "id": str(uuid.uuid4()),
        "expires_at": (timezone.now() + timedelta(minutes=30)).isoformat(),
        "fingerprint": digest(payload),
    }
    statement.save(update_fields=["preview"])
    return statement.preview


@transaction.atomic
def commit(landlord, batch_id, preview_id, *, actor=None):
    statement = locked_statement(landlord, batch_id)
    if statement.receipt and statement.receipt.get("preview_id") == preview_id:
        return statement.receipt
    # A later partial approval must not erase an earlier approval's retry receipt.
    previous = (
        list(
            statement.transactions.filter(result__preview_id=preview_id).order_by(
                "row_number"
            )
        )
        if preview_id
        else []
    )
    if previous:
        results = [{"row_id": row.pk, **row.result} for row in previous]
        return {
            "preview_id": preview_id,
            "batch_id": str(statement.batch_id),
            "verified": True,
            "results": results,
            "remaining": results[0]["remaining_after"],
            "affected_entry_ids": [
                ident for result in results for ident in result["entry_ids"]
            ],
        }
    editable(statement)
    approved = statement.preview
    if (
        not approved
        or approved.get("id") != preview_id
        or timezone.now().isoformat() > approved["expires_at"]
    ):
        raise Error("This approval expired or changed. Prepare a fresh preview.")
    current = build_preview(statement, [item["row_id"] for item in approved["effects"]])
    if digest(current) != approved["fingerprint"]:
        raise Error(
            "Records changed since preview. Review a fresh preview before approving."
        )
    results = []
    for item in current["effects"]:
        row = statement.transactions.get(pk=item["row_id"])
        effect, entries = item["effect"], []
        action = effect["action"]
        if action == "payment":
            for allocation in effect["allocations"]:
                entry, _ = services.record_payment(
                    charge=resolve_entry(landlord, allocation["entry_id"]),
                    amount=allocation["amount"],
                    payment_date=row.booked_on,
                    payment_method="OTHER",
                    reference_number=row.reference[:100],
                    idempotency_key=f"bank:{row.pk}:{allocation['entry_id']}",
                    created_by=actor,
                    notes=row.description[:255],
                )
                entries.append(entry)
        elif action in {"pay_expense", "link"}:
            entry = resolve_entry(landlord, effect["entry_id"])
            if action == "pay_expense":
                services.mark_expense_paid(
                    entry, paid_on=row.booked_on, created_by=actor
                )
                entry.refresh_from_db()
            entries.append(entry)
        elif action == "expense":
            from rentium.properties.models import Property, PropertyHolding

            entry, _ = services.post_expense(
                landlord=landlord,
                amount=effect["amount"],
                category=effect["category"],
                description=effect["description"],
                incurred_date=date.fromisoformat(effect["incurred_date"]),
                paid_on=row.booked_on,
                property=Property.objects.get(pk=effect["property_id"])
                if effect["property_id"]
                else None,
                holding=PropertyHolding.objects.get(pk=effect["holding_id"])
                if effect["holding_id"]
                else None,
                vendor=effect["vendor"],
                created_by=actor,
                idempotency_key=f"bank:{row.pk}:expense",
                metadata={"bank_row": row.pk, "import_batch": str(statement.batch_id)},
            )
            entries.append(entry)
        if effect.get("document"):
            from rentium.rama.document_services import _link_to_existing_expense, _event
            from rentium.rama.models import RamaDocument, RamaDocumentEvent

            doc = RamaDocument.objects.get(
                pk=effect["document"]["id"], landlord=landlord
            )
            doc.ledger_entry = _link_to_existing_expense(doc, str(entries[0].pk), actor)
            doc.save(update_fields=["ledger_entry", "updated_at"])
            services._sync_source_document_payment_state(entries[0])
            _event(
                doc,
                RamaDocumentEvent.Kind.EXPENSE_POSTED,
                actor=actor,
                ledger_entry_id=str(entries[0].pk),
                bank_row=row.pk,
            )
        if action != "exclude":
            if sum(e.amount for e in entries) != abs(row.signed_amount):
                raise Error(
                    "Post-write verification failed; the batch was rolled back."
                )
            if any(e.landlord_id != landlord.pk or e.voided for e in entries):
                raise Error("Post-write ownership/ledger verification failed.")
        row.result = {
            "action": action,
            "entry_ids": [str(e.pk) for e in entries],
            "verified": True,
            "preview_id": preview_id,
            "approved_by": str(actor.pk) if actor else None,
            "approved_at": timezone.now().isoformat(),
            "effect": effect,
            "remaining_after": current["remaining"],
        }
        row.save(update_fields=["result"])
        results.append({"row_id": row.pk, **row.result})
    remaining = statement.transactions.filter(result={}).count()
    statement.receipt = {
        "preview_id": preview_id,
        "batch_id": str(statement.batch_id),
        "verified": True,
        "results": results,
        "remaining": remaining,
        "affected_entry_ids": [ident for r in results for ident in r["entry_ids"]],
    }
    statement.preview = {}
    statement.revision += 1
    statement.save(update_fields=["receipt", "preview", "revision"])
    if not remaining:
        statement.batch.status = ImportBatch.Status.COMMITTED
        statement.batch.committed_at = timezone.now()
        statement.batch.save(update_fields=["status", "committed_at"])
    return statement.receipt
