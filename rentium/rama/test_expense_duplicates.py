"""
Two paths can record one cost, and neither could see the other.

The chat path (create_expense) writes with no idempotency key. The document
path (file_document) keys only on its own document id. The sha256 check on
upload catches the same FILE twice — it cannot catch the same COST arriving
once by message and once by receipt photo, which is how these actually double.

Scenario throughout: $31.45 of mulch is mentioned in chat on day one, and the
receipt is photographed and uploaded on day two.
"""

from __future__ import annotations

import datetime

import pytest

from rentium.ledger import services as ledger_services
from rentium.ledger.models import EntryType, ExpenseCategory, LedgerEntry
from rentium.rama import registry

pytestmark = pytest.mark.django_db

MULCH = "31.45"


def _holding(landlord):
    from rentium.properties.models import PropertyHolding

    return PropertyHolding.objects.create(
        landlord=landlord, name="950 McKenzie Ave", address="950 McKenzie Ave"
    )


def _chat_expense(landlord, holding, amount=MULCH, description="Mulch"):
    out = registry.execute(
        "create_expense",
        {
            "amount": amount,
            "description": description,
            "holding_name": holding.name,
            "confirm": "yes",
        },
        landlord=landlord,
    )
    return LedgerEntry.objects.get(pk=out["expense"]["id"])


# --------------------------------------------------------------- the finder
def test_the_same_cost_a_day_later_is_flagged(landlord):
    holding = _holding(landlord)
    _chat_expense(landlord, holding)

    found = ledger_services.find_duplicate_expense_candidates(
        landlord,
        amount=MULCH,
        on_date=datetime.date.today() + datetime.timedelta(days=1),
        holding=holding,
    )
    assert len(found) == 1
    assert found[0]["description"] == "Mulch"
    assert found[0]["same_scope"] is True
    assert found[0]["has_document"] is False


def test_a_different_amount_is_not_flagged(landlord):
    holding = _holding(landlord)
    _chat_expense(landlord, holding)

    assert ledger_services.find_duplicate_expense_candidates(
        landlord, amount="10.00", holding=holding
    ) == []


def test_the_same_amount_months_later_is_not_flagged(landlord):
    """A recurring cost of the same size must not nag forever."""
    holding = _holding(landlord)
    _chat_expense(landlord, holding)

    assert ledger_services.find_duplicate_expense_candidates(
        landlord,
        amount=MULCH,
        on_date=datetime.date.today() + datetime.timedelta(days=90),
        holding=holding,
    ) == []


def test_a_scope_mismatch_is_still_reported(landlord):
    """Recording portfolio-wide then filing the receipt against the holding is
    a common way to double up — flag it, but say the scope differs."""
    holding = _holding(landlord)
    registry.execute(
        "create_expense",
        {"amount": MULCH, "description": "Mulch", "confirm": "yes"},
        landlord=landlord,
    )

    found = ledger_services.find_duplicate_expense_candidates(
        landlord, amount=MULCH, holding=holding
    )
    assert len(found) == 1
    assert found[0]["same_scope"] is False


def test_candidates_are_landlord_scoped(landlord, other_landlord):
    holding = _holding(landlord)
    _chat_expense(landlord, holding)

    assert ledger_services.find_duplicate_expense_candidates(
        other_landlord, amount=MULCH
    ) == []


# ------------------------------------------------------------- the chat path
def test_the_preview_warns_before_the_money_is_on_the_books(landlord):
    holding = _holding(landlord)
    _chat_expense(landlord, holding)

    preview = registry.execute(
        "create_expense",
        {"amount": MULCH, "description": "Mulch again", "holding_name": holding.name},
        landlord=landlord,
    )
    assert preview["needs_confirm"] is True
    assert preview["preview"]["possible_duplicates"]
    assert "same cost being recorded twice" in preview["preview"]["duplicate_warning"]


def test_a_first_expense_previews_without_a_warning(landlord):
    holding = _holding(landlord)

    preview = registry.execute(
        "create_expense",
        {"amount": MULCH, "description": "Mulch", "holding_name": holding.name},
        landlord=landlord,
    )
    assert "possible_duplicates" not in preview["preview"]


def test_the_warning_does_not_block_a_genuine_second_cost(landlord):
    """Advisory, not a veto — confirming still records it."""
    holding = _holding(landlord)
    _chat_expense(landlord, holding)
    _chat_expense(landlord, holding, description="More mulch")

    assert (
        LedgerEntry.objects.filter(
            landlord=landlord, entry_type=EntryType.EXPENSE
        ).count()
        == 2
    )


# --------------------------------------------------------- the document path
def _receipt(landlord, holding, amount=MULCH):
    from django.core.files.base import ContentFile

    from rentium.rama.models import RamaDocument

    document = RamaDocument(
        landlord=landlord,
        holding=holding,
        original_filename="mulch-receipt.jpg",
        media_type="image/jpeg",
        byte_size=1024,
        sha256="a" * 64,
        kind=RamaDocument.Kind.EXPENSE,
        title="Mulch",
        amount=amount,
        expense_category=ExpenseCategory.MAINTENANCE,
        payment_state=RamaDocument.PaymentState.PAID,
        document_date=datetime.date.today(),
    )
    document.original_file.save("mulch-receipt.jpg", ContentFile(b"x"), save=False)
    document.save()
    return document


def test_filing_a_receipt_for_an_existing_expense_refuses_and_offers_the_link(
    landlord, django_user_model
):
    from rentium.rama.document_services import DuplicateExpenseError, file_document

    holding = _holding(landlord)
    entry = _chat_expense(landlord, holding)
    document = _receipt(landlord, holding)

    with pytest.raises(DuplicateExpenseError) as exc:
        file_document(document, actor=landlord.user)

    assert exc.value.candidates
    assert exc.value.candidates[0]["id"] == str(entry.pk)
    # Nothing was posted.
    assert (
        LedgerEntry.objects.filter(
            landlord=landlord, entry_type=EntryType.EXPENSE
        ).count()
        == 1
    )


def test_linking_attaches_the_receipt_without_posting_a_second_expense(landlord):
    from rentium.rama.document_services import file_document

    holding = _holding(landlord)
    entry = _chat_expense(landlord, holding)
    document = _receipt(landlord, holding)

    file_document(
        document, actor=landlord.user, duplicate_resolution=f"link:{entry.pk}"
    )

    document.refresh_from_db()
    assert document.ledger_entry_id == entry.pk
    assert (
        LedgerEntry.objects.filter(
            landlord=landlord, entry_type=EntryType.EXPENSE
        ).count()
        == 1
    )


def test_confirming_it_is_separate_posts_it(landlord):
    from rentium.rama.document_services import file_document

    holding = _holding(landlord)
    _chat_expense(landlord, holding)
    document = _receipt(landlord, holding)

    file_document(document, actor=landlord.user, duplicate_resolution="new")

    assert (
        LedgerEntry.objects.filter(
            landlord=landlord, entry_type=EntryType.EXPENSE
        ).count()
        == 2
    )


def test_a_receipt_with_no_match_files_normally(landlord):
    from rentium.rama.document_services import file_document

    holding = _holding(landlord)
    document = _receipt(landlord, holding)

    file_document(document, actor=landlord.user)

    document.refresh_from_db()
    assert document.ledger_entry_id is not None


def test_cannot_link_to_another_landlords_expense(landlord, other_landlord):
    from rentium.rama.document_services import DocumentError, file_document

    holding = _holding(landlord)
    other_holding = _holding(other_landlord)
    stolen = _chat_expense(other_landlord, other_holding)
    document = _receipt(landlord, holding)

    with pytest.raises(DocumentError):
        file_document(
            document, actor=landlord.user, duplicate_resolution=f"link:{stolen.pk}"
        )


@pytest.fixture
def other_landlord():
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())
