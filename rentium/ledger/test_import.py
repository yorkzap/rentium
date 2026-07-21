"""
Historical import: CSV parsing, mapping, deterministic validation, and
commit (staged -> real, append-only ledger entries) via the same
ledger/services writers everything else uses.
"""

from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from . import import_services
from .models import EntryType, ImportBatch, StagedLedgerEntry

pytestmark = pytest.mark.django_db


def _client_for(profile):
    client = APIClient()
    client.force_authenticate(user=profile.user)
    return client


CSV = (
    "Date,Type,Property,Amount,Description\n"
    "2026-05-01,Rent,Oak Ave Suite B,850.00,May rent charge\n"
    "2026-05-03,Payment,Oak Ave Suite B,850.00,May rent paid\n"
    "2026-05-10,Expense,Oak Ave Suite B,120.00,Plumber\n"
)


def _batch(landlord):
    return ImportBatch.objects.create(landlord=landlord, source_filename="test.csv")


# ------------------------------------------------------------ parsing/mapping
def test_read_csv_rows_and_guess_map():
    headers, rows = import_services.read_csv_rows(CSV.encode("utf-8"))
    assert headers == ["Date", "Type", "Property", "Amount", "Description"]
    assert len(rows) == 3
    guess = import_services.guess_column_map(headers)
    assert guess == {
        "Date": "effective_date", "Type": "entry_type", "Property": "property_name",
        "Amount": "amount", "Description": "description",
    }


def test_parse_amount_handles_currency_and_parens():
    assert import_services._parse_amount("$1,300.50") == 1300.50
    assert import_services._parse_amount("(50.00)") == 50  # accounting negative -> positive
    assert import_services._parse_amount("") is None
    assert import_services._parse_amount("garbage") is None


def test_parse_date_multiple_formats():
    assert import_services._parse_date("2026-05-01") == date(2026, 5, 1)
    assert import_services._parse_date("05/01/2026") == date(2026, 5, 1)
    assert import_services._parse_date("") is None
    assert import_services._parse_date("not a date") is None


# --------------------------------------------------------------- staging
def test_stage_rows_resolves_property_and_flags_settlement_link(landlord, bc_property):
    batch = _batch(landlord)
    headers, rows = import_services.read_csv_rows(CSV.encode("utf-8"))
    col_map = import_services.guess_column_map(headers)
    staged = import_services.stage_rows(batch, headers, rows, col_map)
    assert len(staged) == 3

    charge_row = staged[0]
    assert charge_row.entry_type == EntryType.RENT_CHARGE
    assert charge_row.property_id == bc_property.pk
    assert charge_row.due_date == date(2026, 5, 1)
    assert charge_row.issues == []  # a resolvable charge row is clean

    payment_row = staged[1]
    assert payment_row.entry_type == EntryType.PAYMENT
    # No settles_row picked yet — CSVs don't encode this; landlord must link
    # it in review. This is the ONE thing every payment row needs.
    assert any(i["field"] == "settles_row" for i in payment_row.issues)

    expense_row = staged[2]
    assert expense_row.entry_type == EntryType.EXPENSE
    assert expense_row.issues == []


def test_stage_rows_flags_unresolved_property(landlord):
    batch = _batch(landlord)
    csv_text = "Date,Type,Property,Amount\n2026-05-01,Rent,Nonexistent Place,850.00\n"
    headers, rows = import_services.read_csv_rows(csv_text.encode())
    staged = import_services.stage_rows(batch, headers, rows, {
        "Date": "effective_date", "Type": "entry_type", "Property": "property_name", "Amount": "amount",
    })
    assert any(i["field"] == "property" for i in staged[0].issues)


def test_stage_rows_flags_duplicate_of_existing_ledger(landlord, bc_property, bc_lease):
    from . import services as ledger_services

    ledger_services.post_charge(
        landlord=landlord, tenant=None, lease=bc_lease, property=bc_property,
        amount="850.00", due_date=date(2026, 5, 1), entry_type=EntryType.RENT_CHARGE,
        description="Existing rent",
    )
    batch = _batch(landlord)
    headers, rows = import_services.read_csv_rows(CSV.encode())
    staged = import_services.stage_rows(batch, headers, rows, import_services.guess_column_map(headers))
    dupe_msgs = [i["message"] for i in staged[0].issues]
    assert any("duplicate" in m.lower() for m in dupe_msgs)


def test_restaging_replaces_previous_rows(landlord, bc_property):
    batch = _batch(landlord)
    headers, rows = import_services.read_csv_rows(CSV.encode())
    col_map = import_services.guess_column_map(headers)
    import_services.stage_rows(batch, headers, rows, col_map)
    assert StagedLedgerEntry.objects.filter(batch=batch).count() == 3
    import_services.stage_rows(batch, headers, rows[:1], col_map)
    assert StagedLedgerEntry.objects.filter(batch=batch).count() == 1


# --------------------------------------------------------------- validation
def test_validate_row_deposit_return_unsupported(landlord):
    batch = _batch(landlord)
    row = StagedLedgerEntry(
        batch=batch, entry_type=EntryType.DEPOSIT_RETURN, amount=Decimal("100.00"),
        effective_date=date.today(),
    )
    issues = import_services.validate_row(row)
    assert any("isn't supported" in i["message"].lower() for i in issues)


def test_validate_row_charge_needs_due_date(landlord):
    batch = _batch(landlord)
    row = StagedLedgerEntry(batch=batch, entry_type=EntryType.RENT_CHARGE, amount=Decimal("850.00"))
    issues = import_services.validate_row(row)
    assert any(i["field"] == "due_date" for i in issues)


# ------------------------------------------------------------------ commit
def test_commit_batch_posts_charge_and_settled_payment(landlord, bc_property, bc_lease):
    batch = _batch(landlord)
    headers, rows = import_services.read_csv_rows(CSV.encode())
    col_map = import_services.guess_column_map(headers)
    staged = import_services.stage_rows(batch, headers, rows, col_map)
    charge_row, payment_row, expense_row = staged

    # Landlord links the payment to its charge in review.
    payment_row.settles_row = charge_row
    payment_row.payment_method = "ETRANSFER"
    payment_row.issues = import_services.validate_row(payment_row)
    payment_row.save()
    assert payment_row.issues == []

    report = import_services.commit_batch(batch)
    assert report["committed"] == 3
    assert report["errors"] == []
    assert report["batch_status"] == ImportBatch.Status.COMMITTED

    from .models import EntryType as ET, LedgerEntry

    assert LedgerEntry.objects.filter(
        landlord=landlord, entry_type=ET.RENT_CHARGE, amount="850.00"
    ).exists()
    posted_payment = LedgerEntry.objects.get(landlord=landlord, entry_type=ET.PAYMENT)
    charge_row.refresh_from_db()
    assert posted_payment.settles_id == charge_row.committed_entry_id
    assert LedgerEntry.objects.filter(landlord=landlord, entry_type=ET.EXPENSE).exists()

    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMMITTED
    assert batch.committed_at is not None


def test_commit_batch_skips_issue_rows_and_stays_draft(landlord, bc_property, bc_lease):
    batch = _batch(landlord)
    headers, rows = import_services.read_csv_rows(CSV.encode())
    col_map = import_services.guess_column_map(headers)
    import_services.stage_rows(batch, headers, rows, col_map)
    # payment_row still has its settles_row issue unresolved.

    report = import_services.commit_batch(batch)
    assert report["committed"] == 2  # charge + expense; payment blocked
    assert report["blocked_by_issues"] == 1
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.DRAFT  # not all rows settled


def test_commit_batch_is_idempotent(landlord, bc_property, bc_lease):
    batch = _batch(landlord)
    headers, rows = import_services.read_csv_rows(CSV.encode())
    col_map = import_services.guess_column_map(headers)
    staged = import_services.stage_rows(batch, headers, rows, col_map)
    staged[1].settles_row = staged[0]
    staged[1].payment_method = "ETRANSFER"
    staged[1].issues = import_services.validate_row(staged[1])
    staged[1].save()

    first = import_services.commit_batch(batch)
    assert first["committed"] == 3

    from .models import LedgerEntry

    count_after_first = LedgerEntry.objects.filter(landlord=landlord).count()
    second = import_services.commit_batch(batch)
    assert second["committed"] == 0  # already-committed rows are skipped
    assert LedgerEntry.objects.filter(landlord=landlord).count() == count_after_first


def test_commit_batch_refuses_discarded(landlord):
    from .services import LedgerError

    batch = _batch(landlord)
    batch.status = ImportBatch.Status.DISCARDED
    batch.save()
    with pytest.raises(LedgerError):
        import_services.commit_batch(batch)


# ---------------------------------------------------------------------- API
def test_import_api_upload_map_review_commit_flow(landlord, bc_property, bc_lease):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client = _client_for(landlord)
    upload = SimpleUploadedFile("history.csv", CSV.encode(), content_type="text/csv")
    created = client.post(
        "/api/ledger/import/batches/", {"file": upload, "label": "2026 history"},
        format="multipart",
    )
    assert created.status_code == 201
    body = created.json()
    batch_id = body["id"]
    assert body["headers"] == ["Date", "Type", "Property", "Amount", "Description"]
    assert body["status"] == "DRAFT"

    mapped = client.post(
        f"/api/ledger/import/batches/{batch_id}/mapping/",
        {"column_map": body["guessed_map"]}, format="json",
    )
    assert mapped.status_code == 200
    assert mapped.json()["row_count"] == 3

    rows = client.get(f"/api/ledger/import/batches/{batch_id}/rows/").json()["rows"]
    payment_row_id = rows[1]["id"]
    charge_row_id = rows[0]["id"]
    assert rows[1]["issues"]  # unlinked settlement

    edited = client.patch(
        f"/api/ledger/import/batches/{batch_id}/rows/{payment_row_id}/",
        {"settles_row_id": charge_row_id, "payment_method": "ETRANSFER"},
        format="json",
    )
    assert edited.status_code == 200
    assert edited.json()["issues"] == []

    committed = client.post(f"/api/ledger/import/batches/{batch_id}/commit/")
    assert committed.status_code == 200
    assert committed.json()["committed"] == 3
    assert committed.json()["batch_status"] == "COMMITTED"


def test_import_api_rejects_other_landlords_batch(landlord, other_landlord):
    batch = _batch(landlord)
    client = _client_for(other_landlord)
    res = client.get(f"/api/ledger/import/batches/{batch.pk}/rows/")
    assert res.status_code == 404


def test_import_api_delete_row(landlord, bc_property):
    client = _client_for(landlord)
    batch = _batch(landlord)
    headers, rows = import_services.read_csv_rows(CSV.encode())
    staged = import_services.stage_rows(
        batch, headers, rows, import_services.guess_column_map(headers)
    )
    res = client.delete(
        f"/api/ledger/import/batches/{batch.pk}/rows/{staged[0].pk}/"
    )
    assert res.status_code == 204
    assert not StagedLedgerEntry.objects.filter(pk=staged[0].pk).exists()


@pytest.fixture
def other_landlord():
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())
