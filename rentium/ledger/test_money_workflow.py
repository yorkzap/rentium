from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import pytest
from django.utils import timezone
from openpyxl import Workbook
from rest_framework.test import APIClient

from . import reconciliation as review
from . import services
from .models import BankStatement, EntryType, LedgerEntry
from .money import overview, period_money
from .statement_files import parse_row, read_statement

pytestmark = pytest.mark.django_db
DAY = date(2024, 8, 15)


def statement(landlord, text="2024-08-15,Oak rent,500\n", account="Property chequing"):
    result, _ = review.upload(
        landlord, ("Date,Description,Amount\n" + text).encode(), "bank.csv", account
    )
    review.map_statement(
        landlord,
        result["id"],
        {"Date": "date", "Description": "description", "Amount": "amount"},
        "%Y-%m-%d",
    )
    return BankStatement.objects.get(batch_id=result["id"])


def charge(landlord, prop, amount="850"):
    from rentium.users.models import TenantProfile
    from rentium.users.tests.factories import UserFactory

    tenant = TenantProfile.objects.create(user=UserFactory())
    return services.post_charge(
        landlord=landlord,
        tenant=tenant,
        property=prop,
        amount=amount,
        entry_type=EntryType.RENT_CHARGE,
        due_date=DAY,
        description="Oak rent",
    )[0]


def select_payment(landlord, source, entry, amount="500"):
    row = source.transactions.first()
    review.decide(
        landlord,
        source.batch_id,
        row.pk,
        {
            "action": "payment",
            "allocations": [{"entry_id": str(entry.pk), "amount": amount}],
        },
    )
    return row


def test_statement_to_existing_charge_is_atomic_idempotent_and_agrees(
    landlord, bc_property
):
    entry = charge(landlord, bc_property)
    source = statement(landlord)
    row = select_payment(landlord, source, entry)
    plan = review.preview(landlord, source.batch_id, [row.pk])
    assert not LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).exists()
    result = review.commit(landlord, source.batch_id, plan["id"], actor=landlord.user)
    assert result["verified"] is True
    assert (
        review.commit(landlord, source.batch_id, plan["id"], actor=landlord.user)
        == result
    )
    assert LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).count() == 1
    assert services.outstanding_on(entry) == Decimal("350")
    from rentium.rama.union import month_money

    period = period_money(landlord, date(2024, 8, 1), date(2024, 9, 1))
    assert period["collected_income"] == "500.00"
    assert (
        month_money(landlord, date(2024, 8, 1), date(2024, 9, 1))["recorded_cash_in"]
        == period["recorded_cash_in"]
    )
    client = APIClient()
    client.force_authenticate(landlord.user)
    response = client.get("/api/ledger/money-overview/?start=2024-08-01&end=2024-09-01")
    assert response.status_code == 200, response.data
    assert response.data["period"] == period


def test_changed_balance_rejects_whole_preview(landlord, bc_property):
    entry = charge(landlord, bc_property)
    source = statement(landlord)
    row = select_payment(landlord, source, entry)
    plan = review.preview(landlord, source.batch_id, [row.pk])
    services.record_payment(
        charge=entry, amount="100", payment_method="CASH", payment_date=DAY
    )
    with pytest.raises(services.LedgerError, match="changed"):
        review.commit(landlord, source.batch_id, plan["id"])
    assert LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).count() == 1


def test_preview_expires_but_draft_survives(landlord, bc_property):
    source = statement(landlord)
    row = select_payment(landlord, source, charge(landlord, bc_property))
    plan = review.preview(landlord, source.batch_id, [row.pk])
    with patch(
        "rentium.ledger.reconciliation.timezone.now",
        return_value=timezone.now() + timedelta(minutes=31),
    ):
        with pytest.raises(services.LedgerError, match="expired"):
            review.commit(landlord, source.batch_id, plan["id"])
    row.refresh_from_db()
    assert row.decision["action"] == "payment" and not row.result


def test_split_payment_and_batch_overallocation(landlord, bc_property):
    a, b = charge(landlord, bc_property, "200"), charge(landlord, bc_property, "400")
    source = statement(landlord)
    row = source.transactions.first()
    review.decide(
        landlord,
        source.batch_id,
        row.pk,
        {
            "action": "payment",
            "allocations": [
                {"entry_id": str(a.pk), "amount": "200"},
                {"entry_id": str(b.pk), "amount": "300"},
            ],
        },
    )
    plan = review.preview(landlord, source.batch_id, [row.pk])
    result = review.commit(landlord, source.batch_id, plan["id"])
    assert len(result["affected_entry_ids"]) == 2
    assert services.outstanding_on(a) == 0 and services.outstanding_on(b) == 100


def test_selected_rows_cannot_collect_more_than_charge(landlord, bc_property):
    entry = charge(landlord, bc_property, "800")
    source = statement(landlord, "2024-08-15,Oak rent,500\n2024-08-16,Oak rent,500\n")
    for row in source.transactions.all():
        review.decide(
            landlord,
            source.batch_id,
            row.pk,
            {
                "action": "payment",
                "allocations": [{"entry_id": str(entry.pk), "amount": "500"}],
            },
        )
    with pytest.raises(services.LedgerError, match="together exceed"):
        review.preview(
            landlord,
            source.batch_id,
            list(source.transactions.values_list("pk", flat=True)),
        )


def test_writer_failure_rolls_back_every_effect(landlord, bc_property):
    source = statement(
        landlord, "2024-08-15,First bill,-50\n2024-08-16,Second bill,-60\n"
    )
    for row in source.transactions.all():
        review.decide(
            landlord,
            source.batch_id,
            row.pk,
            {
                "action": "expense",
                "category": "UTILITIES",
                "property_id": str(bc_property.pk),
            },
        )
    plan = review.preview(
        landlord,
        source.batch_id,
        list(source.transactions.values_list("pk", flat=True)),
    )
    original = services.post_expense
    count = 0

    def fail_second(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise services.LedgerError("test failure")
        return original(**kwargs)

    with patch.object(services, "post_expense", side_effect=fail_second):
        with pytest.raises(services.LedgerError, match="test failure"):
            review.commit(landlord, source.batch_id, plan["id"])
    assert not LedgerEntry.objects.filter(entry_type=EntryType.EXPENSE).exists()
    assert not source.transactions.exclude(result={}).exists()


def test_statement_reupload_and_overlapping_file_are_not_double_posted(
    landlord, bc_property
):
    entry = charge(landlord, bc_property)
    source = statement(landlord)
    row = select_payment(landlord, source, entry)
    plan = review.preview(landlord, source.batch_id, [row.pk])
    review.commit(landlord, source.batch_id, plan["id"])
    payload, created = review.upload(
        landlord,
        b"Date,Description,Amount\n2024-08-15,Oak rent,500\n",
        "renamed.csv",
        "Property chequing",
    )
    assert not created and payload["id"] == str(source.batch_id)
    second = statement(landlord, "2024-08-15,Oak rent,500\n2024-08-16,Unknown,20\n")
    target = second.transactions.first()
    review.decide(
        landlord,
        second.batch_id,
        target.pk,
        {
            "action": "payment",
            "allocations": [{"entry_id": str(entry.pk), "amount": "500"}],
        },
    )
    with pytest.raises(services.LedgerError, match="already reviewed"):
        review.preview(landlord, second.batch_id, [target.pk])


def test_cash_uses_clear_date_and_incurred_view_stays_distinct(landlord, bc_property):
    services.post_expense(
        landlord=landlord,
        property=bc_property,
        amount="75",
        category="UTILITIES",
        description="July bill",
        incurred_date=date(2024, 7, 20),
        paid_on=DAY,
    )
    services.post_expense(
        landlord=landlord,
        property=bc_property,
        amount="40",
        category="UTILITIES",
        description="Unpaid August bill",
        incurred_date=DAY,
    )
    result = overview(landlord, date(2024, 8, 1), date(2024, 9, 1))
    assert result["period"]["expenses"] == "40.00"
    assert result["period"]["expenses_cleared"] == "75.00"
    assert result["period"]["recorded_cash_movement"] == "-75.00"
    assert result["unpaid_bills"] == "40.00"


def test_owner_isolation_blocks_foreign_allocation_and_statement(landlord, bc_property):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    other = LandlordProfile.objects.create(user=UserFactory())
    source = statement(other)
    entry = charge(landlord, bc_property)
    row = select_payment(other, source, entry)
    with pytest.raises(services.LedgerError, match="outside this portfolio"):
        review.preview(other, source.batch_id, [row.pk])
    with pytest.raises(services.LedgerError, match="not found"):
        review.preview(landlord, source.batch_id, [row.pk])


def test_receipt_and_bank_row_link_one_existing_expense(landlord, bc_property):
    from rentium.rama.models import RamaDocument

    expense = services.post_expense(
        landlord=landlord,
        property=bc_property,
        amount="50",
        category="SUPPLIES",
        description="Hardware",
        incurred_date=DAY,
    )[0]
    doc = RamaDocument.objects.create(
        landlord=landlord,
        original_filename="receipt.pdf",
        sha256="a" * 64,
        amount="50",
        title="Hardware",
        status=RamaDocument.Status.READY,
    )
    source = statement(landlord, "2024-08-15,Hardware,-50\n")
    row = source.transactions.first()
    review.decide(
        landlord,
        source.batch_id,
        row.pk,
        {
            "action": "pay_expense",
            "entry_id": str(expense.pk),
            "document_id": str(doc.pk),
        },
    )
    plan = review.preview(landlord, source.batch_id, [row.pk])
    review.commit(landlord, source.batch_id, plan["id"])
    expense.refresh_from_db()
    doc.refresh_from_db()
    assert expense.paid_on == DAY and doc.ledger_entry_id == expense.pk
    assert LedgerEntry.objects.filter(entry_type=EntryType.EXPENSE).count() == 1


def test_invalid_source_never_becomes_zero_or_expense(landlord):
    source = statement(landlord, "08/09/2024,Ambiguous date,NaN\n")
    row = source.transactions.first()
    assert row.parse_error and row.signed_amount is None
    with pytest.raises(services.LedgerError):
        review.preview(landlord, source.batch_id, [row.pk])


def test_xlsx_tab_selection_and_typed_dates():
    workbook = Workbook()
    workbook.active.title = "Notes"
    workbook.active.append(["Read me"])
    tab = workbook.create_sheet("Transactions")
    tab.append(["Date", "Amount"])
    tab.append([DAY, -50])
    output = BytesIO()
    workbook.save(output)
    headers, rows, sheets, selected = read_statement(
        output.getvalue(), "bank.xlsx", "Transactions"
    )
    assert selected == "Transactions" and "Notes" in sheets
    booked, amount, _, _ = parse_row(
        rows[0], dict(zip(headers, ["date", "amount"])), "%Y-%m-%d"
    )
    assert booked == DAY and amount == Decimal("-50")


def test_rama_preview_freezes_exact_batch_and_approval(landlord, bc_property):
    from rentium.rama.money_workflows import reconcile_bank_statement

    source = statement(landlord)
    row = select_payment(landlord, source, charge(landlord, bc_property))
    result = reconcile_bank_statement(landlord, str(source.batch_id), str(row.pk))
    assert result["needs_confirm"] and result["resolved_arguments"]["preview_id"]
    assert LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).count() == 0
    done = reconcile_bank_statement(
        landlord, **result["resolved_arguments"], confirm="yes"
    )
    assert done["verified"] is True


@pytest.mark.django_db(transaction=True)
def test_concurrent_approval_posts_once(landlord, bc_property):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import close_old_connections
    from rentium.users.models import LandlordProfile

    source = statement(landlord)
    row = select_payment(landlord, source, charge(landlord, bc_property))
    plan = review.preview(landlord, source.batch_id, [row.pk])
    barrier = Barrier(2)
    owner_id = landlord.pk

    def approve():
        close_old_connections()
        try:
            owner = LandlordProfile.objects.get(pk=owner_id)
            barrier.wait(timeout=10)
            return review.commit(owner, source.batch_id, plan["id"])
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = list(workers.map(lambda _: approve(), range(2)))
    assert first == second
    assert LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).count() == 1


def test_http_upload_mapping_preview_and_confirm(landlord, bc_property):
    from django.core.files.uploadedfile import SimpleUploadedFile

    charge(landlord, bc_property, "500")
    client = APIClient()
    client.force_authenticate(landlord.user)
    uploaded = client.post(
        "/api/ledger/statements/",
        {
            "account": "Test account",
            "file": SimpleUploadedFile(
                "bank.csv", b"Date,Description,Amount\n2024-08-15,Oak rent,500\n"
            ),
        },
        format="multipart",
    )
    assert uploaded.status_code == 201, uploaded.data
    path = f"/api/ledger/statements/{uploaded.data['id']}/"
    staged = client.post(
        path,
        {
            "action": "mapping",
            "mapping": {
                "Date": "date",
                "Description": "description",
                "Amount": "amount",
            },
            "date_format": "%Y-%m-%d",
        },
        format="json",
    )
    assert staged.status_code == 200, staged.data
    row = staged.data["rows"][0]
    assert not row["issue"] and row["decision"]["action"] == "payment"
    preview = client.post(
        path, {"action": "preview", "row_ids": [row["id"]]}, format="json"
    )
    assert preview.status_code == 200, preview.data
    result = client.post(
        path, {"action": "confirm", "preview_id": preview.data["id"]}, format="json"
    )
    assert result.status_code == 200 and result.data["verified"]
    options = client.get("/api/ledger/review-options/", {"batch": uploaded.data["id"]})
    assert options.status_code == 200, options.data


def test_partial_approvals_preserve_old_retry_receipts(landlord):
    source = statement(landlord, "2024-08-15,Transfer,50\n2024-08-16,Personal,-60\n")
    rows = list(source.transactions.all())
    receipts = []
    for row in rows:
        review.decide(
            landlord,
            source.batch_id,
            row.pk,
            {"action": "exclude", "reason": "personal"},
        )
        plan = review.preview(landlord, source.batch_id, [row.pk])
        receipts.append(review.commit(landlord, source.batch_id, plan["id"]))
    assert receipts[0]["remaining"] == 1
    assert receipts[1]["remaining"] == 0
    assert (
        review.commit(landlord, source.batch_id, receipts[0]["preview_id"])
        == receipts[0]
    )
    assert not LedgerEntry.objects.exists()


def test_large_statement_prepares_in_worker_and_retries_safely(
    landlord, django_capture_on_commit_callbacks
):
    content = "Date,Description,Amount\n" + "2024-08-15,Unknown,50\n" * 101
    uploaded, _ = review.upload(landlord, content.encode(), "large.csv", "Test")
    mapping = {"Date": "date", "Description": "description", "Amount": "amount"}
    with patch("rentium.ledger.tasks.prepare_bank_statement.delay") as queued:
        with django_capture_on_commit_callbacks(execute=True):
            payload = review.prepare_statement(
                landlord, uploaded["id"], mapping, "%Y-%m-%d"
            )
    assert payload["staging"]["state"] == "QUEUED" and not payload["rows"]
    assert queued.call_count == 1
    from rentium.ledger.tasks import prepare_bank_statement

    prepare_bank_statement(*queued.call_args.args)
    source = BankStatement.objects.get(batch_id=uploaded["id"])
    assert source.staging["state"] == "READY"
    assert source.transactions.count() == 101
    assert not LedgerEntry.objects.exists()
    prepare_bank_statement(*queued.call_args.args)
    assert source.transactions.count() == 101


def test_failed_worker_enqueue_retains_original_and_explains_retry(
    landlord, django_capture_on_commit_callbacks
):
    content = "Date,Description,Amount\n" + "2024-08-15,Unknown,50\n" * 101
    uploaded, _ = review.upload(landlord, content.encode(), "large.csv", "Test")
    with patch(
        "rentium.ledger.tasks.prepare_bank_statement.delay",
        side_effect=RuntimeError("broker down"),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            review.prepare_statement(
                landlord,
                uploaded["id"],
                {"Date": "date", "Amount": "amount"},
                "%Y-%m-%d",
            )
    source = BankStatement.objects.get(batch_id=uploaded["id"])
    assert source.staging["state"] == "FAILED"
    with source.batch.source_file.open("rb") as original:
        assert original.read() == content.encode()


def test_invalid_allocation_is_visible_and_rejected_without_500(landlord):
    source = statement(landlord)
    row = source.transactions.first()
    result = review.decide(
        landlord,
        source.batch_id,
        row.pk,
        {"action": "payment", "allocations": ["invalid"]},
    )
    assert "allocation" in result["rows"][0]["issue"].lower()
    client = APIClient()
    client.force_authenticate(landlord.user)
    response = client.post(
        f"/api/ledger/statements/{source.batch_id}/",
        {"action": "preview", "row_ids": [row.pk]},
        format="json",
    )
    assert response.status_code == 400


def test_holding_unit_listing_and_summary_share_settlement_scope(landlord, bc_property):
    from rentium.properties.models import PropertyHolding, PropertyUnit

    holding = PropertyHolding.objects.create(landlord=landlord, name="Address")
    unit = PropertyUnit.objects.create(landlord=landlord, holding=holding, name="Unit")
    bc_property.holding = holding
    bc_property.unit = unit
    bc_property.save(update_fields=["holding", "unit"])
    entry = charge(landlord, bc_property)
    payment, _ = services.record_payment(
        charge=entry, amount="100", payment_date=DAY, payment_method="CASH"
    )
    # Settlement scope, not copied payment columns, owns attribution.
    LedgerEntry.objects.filter(pk=payment.pk).update(property=None, holding=None)
    periods = [
        period_money(landlord, date(2024, 8, 1), date(2024, 9, 1), **{key: value})
        for key, value in [
            ("holding_id", holding.pk),
            ("unit_id", unit.pk),
            ("property_id", bc_property.pk),
        ]
    ]
    assert all(p["collected_income"] == "100.00" for p in periods)
    from rentium.rama.money_workflows import money_overview

    assert (
        money_overview(landlord, "2024-08", holding_id=str(holding.pk))["period"]
        == periods[0]
    )
    client = APIClient()
    client.force_authenticate(landlord.user)
    assert (
        client.get("/api/ledger/summary/", {"property": "not-a-uuid"}).status_code
        == 400
    )
