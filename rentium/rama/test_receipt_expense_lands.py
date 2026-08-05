"""
The last step: an answered question has to become a ledger entry.

The filing preview now states the money, and the landlord answered every
question it asked — correctly, in order:

    "yes but the amount is 37.16 and not 3000"
    "yes, and its left the bank"

and got:

    "I asked you to confirm something I hadn't actually worked out — there's
     no pending action behind that … Tell me again what to record."

Nothing was posted. Two failures stacked:

  * "its left the bank" matched no pattern. `_expense_bank_correction` knew
    "came out of the bank" and "cleared the bank" but not the commonest phrasing
    of all, so the answer to RAMA's own question read as no answer.
  * nothing routed the answer. The document was scoped, its amount corrected
    and its bank status stated, and finishing the job was left to the model —
    which wrote confirmation prose without calling file_business_document. The
    guard caught the empty confirm (correctly) and told a landlord who had just
    supplied everything to start again.

So the answer to the expense question is now parsed and executed in Python,
and the preview it produces is a real one with a plan behind it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from rentium.ledger.models import EntryType, LedgerEntry
from rentium.properties.models import PropertyHolding
from rentium.rama.document_services import (
    catalog_document_scope,
    ingest_document,
    process_document,
)
from rentium.rama.models import RamaDocument

pytestmark = pytest.mark.django_db

SAND_RECEIPT = (
    "How doers get more done\n"
    "8986 SHELBOURNE STREET\n"
    "LANDSCAPING SAND 25KG\n"
    "TOTAL 3000.00\n"
)


@pytest.fixture
def filed_sand_receipt(landlord):
    """Where the transcript had got to: filed to the address, no expense yet."""
    PropertyHolding.objects.create(
        landlord=landlord,
        name="McKenzie House",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    upload = SimpleUploadedFile("receipt.jpg", b"image", content_type="image/jpeg")
    document, _ = ingest_document(landlord=landlord, upload=upload)
    with patch(
        "rentium.rama.document_services._pdf_and_text",
        return_value=(b"%PDF", SAND_RECEIPT),
    ):
        process_document(document.pk)
    catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Ave",
        confirm=True,
    )
    document.refresh_from_db()
    assert document.ledger_entry_id is None
    return document


# ================================================ hearing the answer at all
@pytest.mark.parametrize(
    "said",
    [
        "yes, and its left the bank",              # verbatim from the transcript
        "yes and it's left the bank",
        "it has left my bank",
        "yes it left my account",
        "that went out of my account already",
        "yes, already paid",
        "it went through",
        "the money is gone",
    ],
)
def test_saying_the_money_is_gone_reads_as_paid(said):
    from rentium.rama.service import _expense_bank_correction

    assert _expense_bank_correction(said) == "paid"


@pytest.mark.parametrize(
    "said",
    [
        "no, it hasn't left the bank yet",
        "still unpaid",
        "not yet — it's still owing",
        "leave it unpaid",
        "it hasn't gone out yet",
    ],
)
def test_saying_it_is_still_owing_reads_as_unpaid(said):
    from rentium.rama.service import _expense_bank_correction

    assert _expense_bank_correction(said) == "unpaid"


def test_an_unrelated_message_is_not_a_bank_answer():
    """The parser is consulted on ordinary turns too — it must not read a bank
    status into a sentence that has nothing to do with one."""
    from rentium.rama.service import _expense_bank_correction

    assert _expense_bank_correction("what's the rent on Room D?") is None
    assert _expense_bank_correction("add a new listing") is None


# ============================================== turning the answer into money
CONVERSATION = uuid.uuid4()


def _answer(landlord, document, message):
    from rentium.rama.service import _finish_document_expense_from_answer

    return _finish_document_expense_from_answer(
        landlord, CONVERSATION, document, message,
    )


def test_the_answer_produces_a_real_preview(landlord, filed_sand_receipt):
    """"yes, and its left the bank" is a complete answer. It has to reach
    file_business_document, not a paragraph of prose the guard then retracts."""
    filed_sand_receipt.amount = Decimal("37.16")
    filed_sand_receipt.save(update_fields=["amount"])

    reply = _answer(landlord, filed_sand_receipt, "yes, and its left the bank")

    assert reply is not None
    assert "37.16" in reply
    assert "950 McKenzie Ave" in reply
    assert "already left your bank" in reply.casefold()


def test_the_preview_has_a_plan_behind_it(landlord, filed_sand_receipt):
    """The exact thing that was missing. A preview with no persisted plan is
    what the confirmation guard exists to refuse — rightly, because "yes" would
    land nowhere."""
    from rentium.rama.plan_runner import load_fresh_plan

    filed_sand_receipt.amount = Decimal("37.16")
    filed_sand_receipt.save(update_fields=["amount"])
    _answer(landlord, filed_sand_receipt, "yes, and its left the bank")

    plan = load_fresh_plan(landlord, CONVERSATION)
    assert plan is not None
    steps = plan_steps(plan)
    assert steps[0]["tool"] == "file_business_document"
    assert steps[0]["arguments"]["payment_state"] == "PAID"
    assert steps[0]["arguments"]["amount"] == "37.16"


def plan_steps(plan):
    from rentium.rama.plan_runner import plan_to_payload

    return plan_to_payload(plan)["steps"]


def test_the_guard_no_longer_has_anything_to_retract(landlord, filed_sand_receipt):
    """The retraction was correct given an empty confirm — the fix is to stop
    producing one, not to weaken the guard."""
    from rentium.rama.plan_runner import load_fresh_plan
    from rentium.rama.service import _solicits_confirmation

    filed_sand_receipt.amount = Decimal("37.16")
    filed_sand_receipt.save(update_fields=["amount"])
    reply = _answer(landlord, filed_sand_receipt, "yes, and its left the bank")

    assert _solicits_confirmation(reply)  # it does ask for a yes...
    assert load_fresh_plan(landlord, CONVERSATION) is not None  # ...and it means it


def test_confirming_posts_the_expense(landlord, filed_sand_receipt):
    from rentium.rama.registry import execute

    filed_sand_receipt.amount = Decimal("37.16")
    filed_sand_receipt.save(update_fields=["amount"])
    _answer(landlord, filed_sand_receipt, "yes, and its left the bank")

    execute(
        "file_business_document",
        {
            "document_id": str(filed_sand_receipt.pk),
            "amount": "37.16",
            "payment_state": "PAID",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    entry = LedgerEntry.objects.get(landlord=landlord, entry_type=EntryType.EXPENSE)
    assert entry.amount == Decimal("37.16")
    assert entry.paid_on is not None
    filed_sand_receipt.refresh_from_db()
    assert filed_sand_receipt.ledger_entry_id == entry.pk
    assert filed_sand_receipt.status == RamaDocument.Status.FILED


def test_the_landlords_amount_beats_the_ocr_figure(landlord, filed_sand_receipt):
    """OCR said $3000.00 for a bag of sand; the landlord said $37.16. The
    number that reaches the ledger is theirs."""
    assert filed_sand_receipt.amount == Decimal("3000.00")

    reply = _answer(
        landlord, filed_sand_receipt, "yes but the amount is 37.16 and not 3000",
    )
    filed_sand_receipt.refresh_from_db()
    assert filed_sand_receipt.amount == Decimal("37.16")
    assert "3000" not in (reply or "")


def test_an_amount_with_no_bank_answer_still_asks(landlord, filed_sand_receipt):
    """Correcting the total does not say whether the money has moved, and
    paid_on is not something to assume — it decides which month the cost lands
    in."""
    reply = _answer(
        landlord, filed_sand_receipt, "yes but the amount is 37.16 and not 3000",
    )
    assert "left your bank" in (reply or "")
    assert not LedgerEntry.objects.filter(
        landlord=landlord, entry_type=EntryType.EXPENSE
    ).exists()


def test_nothing_is_posted_without_the_confirmation(landlord, filed_sand_receipt):
    filed_sand_receipt.amount = Decimal("37.16")
    filed_sand_receipt.save(update_fields=["amount"])
    _answer(landlord, filed_sand_receipt, "yes, and its left the bank")
    assert not LedgerEntry.objects.filter(
        landlord=landlord, entry_type=EntryType.EXPENSE
    ).exists()


def test_a_document_already_expensed_is_left_alone(landlord, filed_sand_receipt):
    """No second entry for one purchase, whatever the landlord says next."""
    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import ExpenseCategory

    entry, _ = ledger_services.post_expense(
        landlord=landlord,
        holding=filed_sand_receipt.holding,
        amount=Decimal("37.16"),
        category=ExpenseCategory.OTHER,
        description="Landscaping sand",
    )
    filed_sand_receipt.ledger_entry = entry
    filed_sand_receipt.save(update_fields=["ledger_entry"])

    assert _answer(landlord, filed_sand_receipt, "yes, and its left the bank") is None
