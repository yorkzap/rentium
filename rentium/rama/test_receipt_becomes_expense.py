"""
A filed receipt whose money never reached the ledger.

    "Bought landscaping sand for Mckenzie house driveway"

    Ready to store this as a business document for the physical property:
    • Address: 950 McKenzie Ave
    • Individual listing: none
    • Child listings under it: Bonus room J, Garden Suite, McKenzie I, …
    Reply yes to apply this filing, or no to cancel.

Filing and expensing are two writes and that preview described only the first.
A landlord who buys sand, photographs the receipt and says yes has every reason
to believe they have recorded the purchase. Their ledger has nothing in it.

This is not hypothetical. The same portfolio already holds:

  * a receipt, FILED, $39.34, no ledger entry — classified OTHER because OCR
    returned "within 90 devs inal packeses pliances end other exceptions" and
    not one keyword survived. kind=OTHER switches off expense_like, which
    switches off every prompt to record the spend.
  * a hand-typed expense, $39.36, for the same purchase. Two cents and two
    unlinked records apart.

Three fixes, tested here: the preview states the money, the confirmed filing
asks the one question that finishes the job, and a total on the page counts as
evidence of an expense when the words did not survive OCR.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from rentium.properties.models import PropertyHolding
from rentium.rama.document_services import (
    catalog_document_scope,
    ingest_document,
    process_document,
)
from rentium.rama.models import RamaDocument

pytestmark = pytest.mark.django_db


# The landscaping-sand receipt, as OCR actually read it: a big-box header, a
# street address that is not the rental, and a total.
SAND_RECEIPT = (
    "How doers get more done\n"
    "8986 SHELBOURNE STREET\n"
    "STORE MGR CARMEN\n"
    "LANDSCAPING SAND 25KG\n"
    "SUBTOTAL 42.86\n"
    "TOTAL 48.00\n"
)

# A till receipt whose words did not survive OCR but whose total did — the
# common shape, and the one the classifier fallback is for.
KEYWORDLESS_RECEIPT = (
    "wu. rone.ce\n"
    "pliances end other\n"
    "39.34\n"
)

# The real one from this portfolio, verbatim: 116 characters of returns policy
# and NOT ONE figure. No classifier can call this an expense, which is why the
# filing-time check reads document.amount instead of trusting the kind.
GARBLED_RECEIPT = (
    "within 90 devs\ninal packeses\npliances end other\n\n"
    "exceptions. See store deteils\n\n"
    "wu. rone.ce/en/returns-and-refunds\n\n"
)


def _holding(landlord):
    return PropertyHolding.objects.create(
        landlord=landlord,
        name="McKenzie House",
        address="950 McKenzie Ave",
        city="Victoria",
    )


def _receipt(landlord, text, name="receipt.jpg"):
    upload = SimpleUploadedFile(name, b"image-bytes", content_type="image/jpeg")
    document, _ = ingest_document(landlord=landlord, upload=upload)
    with patch(
        "rentium.rama.document_services._pdf_and_text",
        return_value=(b"%PDF-archival", text),
    ):
        process_document(document.pk)
    document.refresh_from_db()
    return document


# ====================================== the preview the landlord says yes to
def test_the_filing_preview_states_that_the_spend_is_not_recorded(landlord):
    """The whole complaint. "Reply yes to apply this filing" said nothing about
    money, so yes looked like recording the purchase."""
    _holding(landlord)
    document = _receipt(landlord, SAND_RECEIPT)

    result = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Ave",
    )
    preview = result["preview"]
    assert preview["expense_like"] is True
    assert preview["ledger_expense"] == "NOT recorded yet"
    assert "does not put the spend in your ledger" in preview["warning"]


def test_the_preview_shows_the_amount_before_it_is_confirmed(landlord):
    """OCR read $3,000.00 off a bag of sand in the real incident. A figure that
    wrong has to be visible while the landlord is deciding, not after."""
    _holding(landlord)
    document = _receipt(landlord, SAND_RECEIPT)

    preview = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Ave",
    )["preview"]
    assert preview["amount"] == str(document.amount)
    assert preview["amount_is_ocr_guess"] is True


def test_the_landlord_reads_the_money_in_the_reply(landlord):
    """The preview payload is only half of it — the deterministic reply is what
    reaches a person, and that is where the line was missing."""
    from rentium.rama.service import _document_preview_reply

    _holding(landlord)
    document = _receipt(landlord, SAND_RECEIPT)
    result = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Ave",
    )

    reply = _document_preview_reply(result)
    assert "NOT in your ledger yet" in reply
    assert "does not record the spend" in reply
    assert f"${document.amount}" in reply
    # And it still says what it always said.
    assert "Reply yes to apply this filing" in reply


def test_a_non_expense_document_says_nothing_about_money(landlord):
    """A tenancy notice has no ledger consequence and must not grow one."""
    from rentium.rama.service import _document_preview_reply

    _holding(landlord)
    document = _receipt(
        landlord,
        "NOTICE TO END TENANCY\n950 McKenzie Ave\nEffective September 30",
        name="notice.jpg",
    )
    result = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Ave",
    )
    assert result["preview"]["expense_like"] is False
    reply = _document_preview_reply(result)
    assert "ledger" not in reply.casefold()


# ============================================ what happens after they say yes
def test_confirming_the_filing_asks_the_expense_question(landlord):
    """Prose in relay_instruction was not enough — there is a filed receipt in
    this portfolio whose expense never followed. question_for_user is the one
    channel the persona must relay verbatim and stop on."""
    _holding(landlord)
    document = _receipt(landlord, SAND_RECEIPT)

    result = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Ave",
        confirm=True,
    )
    assert result["catalogued"] is True
    question = result["question_for_user"]
    assert "ledger" in question.casefold() or "expense" in question.casefold()
    assert "left your bank" in question
    assert result["needs"] == "expense_decision"


def test_the_question_carries_the_amount_it_read(landlord):
    """So a wrong OCR total is corrected in the answer rather than posted."""
    _holding(landlord)
    document = _receipt(landlord, SAND_RECEIPT)
    document.refresh_from_db()

    result = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Ave",
        confirm=True,
    )
    assert f"${document.amount}" in result["question_for_user"]
    assert "if ocr got it wrong" in result["question_for_user"].casefold()


def test_filing_still_only_files(landlord):
    """The question is asked, not answered on the landlord's behalf. Posting an
    expense off an unconfirmed OCR figure would be the worse bug."""
    from rentium.ledger.models import EntryType, LedgerEntry

    _holding(landlord)
    document = _receipt(landlord, SAND_RECEIPT)
    catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Ave",
        confirm=True,
    )
    assert not LedgerEntry.objects.filter(
        landlord=landlord, entry_type=EntryType.EXPENSE
    ).exists()


def test_an_already_recorded_spend_is_not_asked_about_twice(landlord):
    """The landlord who says "bought draino, $18.41" and photographs the
    receipt afterwards must not be prompted to post it again."""
    from decimal import Decimal

    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import ExpenseCategory

    holding = _holding(landlord)
    ledger_services.post_expense(
        landlord=landlord,
        holding=holding,
        amount=Decimal("48.00"),
        category=ExpenseCategory.OTHER,
        description="Landscaping sand for the McKenzie driveway",
    )
    document = _receipt(landlord, SAND_RECEIPT)

    result = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Ave",
    )
    preview = result["preview"]
    assert preview.get("matches_existing_expense")
    assert preview.get("ledger_expense") != "NOT recorded yet"


# ================================== the classification that switched it all off
def test_a_receipt_that_ocrd_badly_is_still_an_expense(landlord):
    """A till receipt keeps its money and loses its words. Not one rule keyword
    survives, so it files as OTHER, expense_like goes False, and every prompt
    to record the spend vanishes with it."""
    document = _receipt(landlord, KEYWORDLESS_RECEIPT)
    assert document.kind == RamaDocument.Kind.EXPENSE


def test_the_amount_fallback_is_marked_as_the_weaker_guess(landlord):
    """It IS a weaker signal than a keyword match, and the confidence has to
    say so rather than presenting a guess as a reading."""
    keyword = _receipt(landlord, SAND_RECEIPT)
    fallback = _receipt(landlord, KEYWORDLESS_RECEIPT, name="other.jpg")
    assert (
        fallback.classification_confidence < keyword.classification_confidence
    )


def test_an_amount_on_the_record_beats_the_kind_the_classifier_chose(landlord):
    """The real failed document, verbatim. Its OCR is 116 characters of returns
    policy with NO figure in it, so no classifier could ever call it an expense
    — it filed as OTHER and its $39.34 arrived afterwards, by which point
    nothing was asking about the money. The filing-time check therefore reads
    the amount on the record, not the kind the classifier picked at ingest."""
    from decimal import Decimal

    from rentium.rama.document_services import _money_still_to_record

    _holding(landlord)
    # A camera filename, like the real one — nothing for the classifier there
    # either. (`_classify` reads the filename too, so calling this "receipt.jpg"
    # would have handed the test the answer the incident did not have.)
    document = _receipt(landlord, GARBLED_RECEIPT, name="IMG_4471.jpg")
    assert document.kind == RamaDocument.Kind.OTHER  # correctly — no money on it
    assert document.amount is None

    # The amount arrives later, exactly as it did on the real document.
    document.amount = Decimal("39.34")
    document.save(update_fields=["amount"])

    money = _money_still_to_record(document)
    assert money["expense_like"] is True
    assert money["ledger_expense"] == "NOT recorded yet"


def test_a_bank_statement_full_of_amounts_stays_a_bank_statement(landlord):
    """The obvious way to break this: statements are nothing but money. The
    fallback only ever upgrades FROM Other, never overrides a rule that hit."""
    document = _receipt(
        landlord,
        "SCOTIABANK ACCOUNT STATEMENT\nOPENING BALANCE 1,204.55\n"
        "CLOSING BALANCE 998.12\nTRANSACTION HISTORY\n",
        name="statement.jpg",
    )
    assert document.kind == RamaDocument.Kind.BANK_STATEMENT


def test_a_document_with_no_money_stays_other(landlord):
    document = _receipt(
        landlord,
        "Keys handed over to the new tenant on move-in day.",
        name="note.jpg",
    )
    assert document.kind == RamaDocument.Kind.OTHER


def test_the_persona_says_a_receipt_is_two_writes():
    """The preview and the follow-up question are the net. The persona is what
    stops the model getting there — and it has to be on both prompts, since the
    General fronts the chat and the Corporal holds file_business_document."""
    from rentium.rama.roles import CORPORAL_PROMPT

    flat = " ".join(CORPORAL_PROMPT.split())
    assert "A RECEIPT IS TWO WRITES, NOT ONE" in flat
    assert "NEVER leave a receipt filed with no expense" in flat
    # And the other half: don't post a second expense when one already exists.
    assert "attach the receipt to that entry" in flat
