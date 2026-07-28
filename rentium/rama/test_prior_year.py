"""
Prior-year history: bank statements and uncommitted import batches.

Two dangers, one on each side:

- a bank statement filed as if it were a receipt posts ONE invented expense
  for whatever money figure happened to appear first on the page;
- staged rows added to a ledger total double-count history the landlord has
  not actually committed — and may still delete.

Every test here is about one of those two.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from rentium.ledger.models import ImportBatch, StagedLedgerEntry
from rentium.rama.models import RamaDocument

pytestmark = pytest.mark.django_db

STATEMENT = (
    "COAST CAPITAL SAVINGS — Account Statement\n"
    "Statement period June 1 to June 30, 2026\n"
    "Opening balance $4,210.55\n"
    "Jun 03 E-TRANSFER RECEIVED 950.00\n"
    "Jun 11 HOME DEPOT INVOICE 31.45\n"
    "Jun 27 FORTIS BC subtotal 88.20\n"
    "Closing balance $5,040.90\n"
)


def _classify(text, filename="statement.pdf"):
    from rentium.rama.document_services import _classify as fn

    return fn(text, filename)


# ------------------------------------------------------- classifying a statement
def test_a_statement_is_not_read_as_a_receipt():
    """It contains "invoice" and "subtotal" — the words the expense rule
    matches on — so the ordering of the rules is the whole test."""
    assert _classify(STATEMENT)["kind"] == RamaDocument.Kind.BANK_STATEMENT


def test_a_statement_carries_no_amount():
    """Otherwise the opening balance shows up as the document's cost."""
    assert _classify(STATEMENT)["amount"] is None


def test_a_statement_is_not_expense_like():
    result = _classify(STATEMENT)
    assert result["payment_state"] == RamaDocument.PaymentState.NOT_APPLICABLE
    assert result["category"] == ""


def test_an_actual_receipt_still_classifies_as_an_expense():
    """The guard above must not have swallowed the ordinary case."""
    receipt = "HOME DEPOT\nInvoice #4471\nSubtotal 28.06\nGST 3.39\nAmount due 31.45"
    assert _classify(receipt)["kind"] == RamaDocument.Kind.EXPENSE


def test_filing_can_never_post_money_for_a_statement():
    """file_document posts an expense only for kinds in an explicit set. This
    asserts BANK_STATEMENT stays out of it — a statement is a LIST of
    transactions, so one posting would be one invented charge."""
    import inspect

    from rentium.rama import document_services

    source = inspect.getsource(document_services.file_document)
    posting_block = source.split("if document.amount and document.kind in {")[1]
    posting_block = posting_block.split("}")[0]
    assert "BANK_STATEMENT" not in posting_block


# ------------------------------------------------------------- staged batches
@pytest.fixture
def draft_batch(landlord):
    batch = ImportBatch.objects.create(
        landlord=landlord, label="2025 statements", source_filename="2025.csv"
    )
    for index, (amount, description) in enumerate(
        [("2000.00", "Rent — upstairs"), ("31.45", "Mulch"), ("88.20", "Fortis")]
    ):
        StagedLedgerEntry.objects.create(
            batch=batch,
            row_number=index + 1,
            entry_type="EXPENSE" if index else "PAYMENT",
            amount=Decimal(amount),
            effective_date=datetime.date(2025, 6, index + 1),
            description=description,
        )
    return batch


def test_batches_are_listed_with_their_state(landlord, draft_batch):
    from rentium.rama.finance import list_import_batches

    result = list_import_batches(landlord)
    assert result["count"] == 1
    assert result["batches"][0]["status"] == "DRAFT"
    assert result["batches"][0]["row_count"] == 3


def test_staged_rows_are_labelled_provisional(landlord, draft_batch):
    """The load-bearing field. Without it these read like recorded history."""
    from rentium.rama.finance import read_staged_entries

    result = read_staged_entries(landlord, batch_id=str(draft_batch.pk))
    assert result["provenance"] == "PROVISIONAL"
    assert "never add them to a ledger total" in result["instruction"]
    assert len(result["rows"]) == 3


def test_a_committed_batch_is_not_provisional(landlord, draft_batch):
    """Its rows ARE the ledger, so reading them and adding them would be the
    double-count in the other direction."""
    from rentium.rama.finance import read_staged_entries

    draft_batch.status = ImportBatch.Status.COMMITTED
    draft_batch.save(update_fields=["status"])
    result = read_staged_entries(landlord, batch_id=str(draft_batch.pk))
    assert result["provenance"] == "LEDGER"


def test_no_batch_id_reads_the_newest_draft(landlord, draft_batch):
    from rentium.rama.finance import read_staged_entries

    assert read_staged_entries(landlord)["batch"]["id"] == str(draft_batch.pk)


def test_nothing_uploaded_is_not_an_error(landlord):
    from rentium.rama.finance import read_staged_entries

    result = read_staged_entries(landlord)
    assert result["rows"] == []
    assert "error" not in result


def test_another_landlords_batch_is_invisible(landlord, draft_batch):
    from rentium.rama.finance import list_import_batches, read_staged_entries
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    stranger = LandlordProfile.objects.create(user=UserFactory())
    assert list_import_batches(stranger)["count"] == 0
    assert read_staged_entries(stranger, batch_id=str(draft_batch.pk))["error"] == (
        "no_such_batch"
    )


def test_rows_are_capped(landlord, draft_batch, monkeypatch):
    """A five-year statement import would otherwise be the whole prompt."""
    from rentium.rama import finance

    monkeypatch.setattr(finance, "MAX_STAGED_ROWS", 2)
    result = finance.read_staged_entries(landlord, batch_id=str(draft_batch.pk))
    assert len(result["rows"]) == 2
    assert result["total_rows"] == 3
    assert result["truncated"] is True


def test_a_rows_issues_come_through(landlord, draft_batch):
    """A row that would not commit must not read as settled history."""
    from rentium.rama.finance import read_staged_entries

    row = draft_batch.rows.first()
    row.issues = [{"message": "No property matched."}]
    row.save(update_fields=["issues"])
    result = read_staged_entries(landlord, batch_id=str(draft_batch.pk))
    assert "No property matched." in result["rows"][0]["issues"]


# ------------------------------------------------------------------ the guard
def test_the_treasurer_can_read_them(landlord):
    from rentium.rama.roles import role_tool_schemas

    names = {t["name"] for t in role_tool_schemas("treasurer")}
    assert {"list_import_batches", "read_staged_entries"} <= names


def test_committing_a_batch_is_not_a_tool_in_any_role():
    """The Treasurer must never turn provisional history into ledger rows —
    and neither should any other role by accident. commit_batch is reachable
    from the import UI only."""
    from rentium.rama import registry

    for name in registry.REGISTRY:
        assert "commit" not in name or "batch" not in name


def test_no_treasurer_tool_can_write(landlord):
    """Re-asserted here because this step ADDED tools to that role."""
    from rentium.rama import registry
    from rentium.rama.roles import TREASURER_TOOLS

    for name in TREASURER_TOOLS:
        tool = registry.REGISTRY[name]
        assert "confirm" not in tool.parameters["properties"], name


# --------------------------------------------------- what the analysis sees
def test_the_pack_keeps_provisional_history_apart_from_the_ledger(
    landlord, draft_batch
):
    """The single most important thing in this step: the analysis must be
    able to USE last year's numbers without them contaminating a ledger total."""
    from rentium.rama.deliberation import build_pack

    draft_batch.rows.filter(entry_type="EXPENSE").update(category="UTILITIES")
    pack = build_pack(landlord)

    assert pack["provisional_history"]["provenance"] == "PROVISIONAL"
    assert pack["provisional_history"]["spend_by_category"]["UTILITIES"] > 0
    # ...and the authoritative figure is untouched by it.
    assert pack["annual_spend"].get("UTILITIES") is None


def test_committed_rows_are_not_repeated_as_provisional(landlord, draft_batch):
    """Once committed they are ledger entries; showing them here as well
    would offer the same money to the analysis twice."""
    from rentium.rama.deliberation import build_pack

    draft_batch.status = ImportBatch.Status.COMMITTED
    draft_batch.save(update_fields=["status"])
    assert build_pack(landlord)["provisional_history"] == {}


def test_no_uploads_means_no_provisional_section(landlord):
    from rentium.rama.deliberation import build_pack

    assert build_pack(landlord)["provisional_history"] == {}


def test_staged_income_is_not_counted_as_spend(landlord, draft_batch):
    from rentium.rama.deliberation import build_pack

    draft_batch.rows.all().update(entry_type="PAYMENT")
    assert build_pack(landlord)["provisional_history"] == {}


# ============================================ the Treasurer settings surface
def _client(landlord):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=landlord.user)
    return client


def test_the_profile_starts_without_consent(landlord):
    """Nothing personal is readable until the landlord says so."""
    body = _client(landlord).get("/api/rama/treasurer/").json()
    assert body["profile"]["consented"] is False


def test_consent_can_be_given_and_withdrawn(landlord):
    from rentium.rama.models import LandlordFinancialProfile

    client = _client(landlord)
    body = client.patch(
        "/api/rama/treasurer/",
        {"consented": True, "self_reported_marginal_rate": "31.00"},
        format="json",
    ).json()
    assert body["profile"]["consented"] is True
    assert body["profile"]["self_reported_marginal_rate"] == "31.00"

    body = client.patch(
        "/api/rama/treasurer/", {"consented": False}, format="json"
    ).json()
    assert body["profile"]["consented"] is False
    # Withdrawing stops the field being READ, it does not erase it — otherwise
    # re-consenting would mean typing everything again.
    profile = LandlordFinancialProfile.objects.get(landlord=landlord)
    assert profile.self_reported_marginal_rate is not None
    assert profile.usable is False


def test_a_nonsense_rate_is_a_400_not_a_500(landlord):
    response = _client(landlord).patch(
        "/api/rama/treasurer/",
        {"self_reported_marginal_rate": "about a third"},
        format="json",
    )
    assert response.status_code == 400


def test_open_requests_are_listed(landlord):
    from rentium.rama.models import TreasurerRequest

    TreasurerRequest.objects.create(
        landlord=landlord,
        question="What did the roof cost?",
        why_it_matters="It decides whether the envelope work is already paid for.",
    )
    body = _client(landlord).get("/api/rama/treasurer/").json()
    assert body["requests"][0]["question"] == "What did the roof cost?"
    assert body["requests"][0]["why_it_matters"]


def test_data_gaps_name_what_is_missing(landlord):
    """A percentage tells a landlord nothing about what to go and do."""
    from rentium.properties.models import PropertyHolding

    PropertyHolding.objects.create(
        landlord=landlord, name="950 McKenzie Ave", address="950 McKenzie Ave"
    )
    body = _client(landlord).get("/api/rama/treasurer/").json()
    gap = body["data_gaps"][0]
    assert gap["holding"] == "950 McKenzie Ave"
    assert "a recent valuation" in gap["missing"]
    assert "the mortgage on it" in gap["missing"]


def test_a_complete_holding_has_no_gap(landlord):
    import datetime
    from decimal import Decimal as D

    from rentium.ledger.models import (
        HoldingFinancials,
        HoldingMortgage,
        HoldingValuation,
    )
    from rentium.properties.models import PropertyHolding

    holding = PropertyHolding.objects.create(
        landlord=landlord, name="Complete Ave", address="Complete Ave"
    )
    HoldingFinancials.objects.create(
        holding=holding, landlord=landlord, year_built=1974,
        purchase_price=D("480000"),
    )
    HoldingValuation.objects.create(
        landlord=landlord, holding=holding, as_of=datetime.date.today(),
        amount=D("700000"), basis=HoldingValuation.Basis.BC_ASSESSMENT,
    )
    HoldingMortgage.objects.create(
        landlord=landlord, holding=holding, original_principal=D("360000")
    )
    assert _client(landlord).get("/api/rama/treasurer/").json()["data_gaps"] == []


def test_another_landlords_treasurer_state_is_invisible(landlord):
    from rentium.rama.models import TreasurerRequest
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    TreasurerRequest.objects.create(landlord=landlord, question="private")
    stranger = LandlordProfile.objects.create(user=UserFactory())
    body = _client(stranger).get("/api/rama/treasurer/").json()
    assert body["requests"] == []


def test_the_endpoint_cannot_write_holding_financials(landlord):
    """Deliberate: the agent that concludes "your equity looks strong" must
    not be the one that types in the valuation. Those go through the
    General's confirm-previewed tools."""
    from rentium.properties.models import PropertyHolding

    holding = PropertyHolding.objects.create(
        landlord=landlord, name="950 McKenzie Ave", address="950 McKenzie Ave"
    )
    _client(landlord).patch(
        "/api/rama/treasurer/",
        {"purchase_price": "1", "year_built": 2020, "holding": str(holding.pk)},
        format="json",
    )
    assert not hasattr(holding, "financials") or holding.financials is None
