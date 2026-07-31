"""
Expense scope: listing, unit, whole holding, or portfolio-wide.

Written from a real transcript. Asked to record mulch and gas against the
HOLDING "950 McKenzie Ave", RAMA labelled both "Basement (950 McKenzie Ave) —
shared". The money landed correctly (property=None, holding=McKenzie) but every
label named a unit the landlord had explicitly excluded.

Root cause: create_expense had no holding argument at all, so the resolver fell
through to a token match where each of "950"/"McKenzie"/"Ave" hit
holding__name__icontains. Every unit in the holding survived the filter, there
happened to be exactly one, and len(units) == 1 was treated as an unambiguous
hit. With a SECOND unit the same request would have started erroring instead.
"""

from __future__ import annotations

import pytest

from rentium.rama import registry

pytestmark = pytest.mark.django_db


def _holding(landlord, name="950 McKenzie Ave", address="950 McKenzie Ave"):
    from rentium.properties.models import PropertyHolding

    return PropertyHolding.objects.create(
        landlord=landlord, name=name, address=address, city="Victoria"
    )


def _unit(landlord, holding, name="Basement"):
    from rentium.properties.models import PropertyUnit

    return PropertyUnit.objects.create(landlord=landlord, holding=holding, name=name)


def _expense(landlord, **kwargs):
    return registry.execute("create_expense", kwargs, landlord=landlord)


# ------------------------------------------------------------- the bug itself
def test_holding_scoped_expense_is_labelled_as_the_whole_property(landlord):
    holding = _holding(landlord)
    _unit(landlord, holding)

    preview = _expense(
        landlord, amount="31.45", description="Mulch", holding_name="950 McKenzie Ave"
    )
    assert preview["needs_confirm"] is True
    assert preview["preview"]["property"] == "950 McKenzie Ave — whole property"
    assert "Basement" not in preview["preview"]["property"]


def test_holding_scoped_expense_posts_against_the_holding(landlord):
    from rentium.ledger.models import LedgerEntry

    holding = _holding(landlord)
    _unit(landlord, holding)

    out = _expense(
        landlord,
        amount="31.45",
        description="Mulch",
        holding_name="950 McKenzie Ave",
        confirm="yes",
    )
    assert out["created"] is True
    assert out["expense"]["scope"] == "950 McKenzie Ave — whole property"
    assert "Logged" in (out.get("message") or "")

    entry = LedgerEntry.objects.get(pk=out["expense"]["id"])
    assert entry.property_id is None
    assert entry.holding_id == holding.pk


def test_paid_verbal_expense_sets_paid_on(landlord):
    from datetime import date

    from rentium.ledger.models import LedgerEntry

    holding = _holding(landlord)
    out = _expense(
        landlord,
        amount="18.41",
        description="Draino for 950 McKenzie house",
        holding_name="950 McKenzie Ave",
        paid_on="today",
        category="SUPPLIES",
        confirm="yes",
    )
    assert out["created"] is True
    entry = LedgerEntry.objects.get(pk=out["expense"]["id"])
    assert entry.holding_id == holding.pk
    assert entry.paid_on == date.today()
    assert entry.category == "SUPPLIES"
    assert "18.41" in out["message"]
    assert "Logged" in out["message"]


def test_verbal_expense_intent_parses_bought_paid(landlord):
    from rentium.rama.service import _verbal_expense_intent
    from rentium.rama.service import _write_result_message

    _holding(landlord)
    live = {
        "listings": [
            {"address": "950 McKenzie Ave", "name": "Room A"},
        ]
    }
    intent = _verbal_expense_intent(
        landlord,
        "I bought draino for 950 mckenzie house for $18.41 today and its paid",
        live,
    )
    assert intent is not None
    assert intent["tool"] == "create_expense"
    assert intent["arguments"]["amount"] == "18.41"
    assert intent["arguments"]["paid_on"] == "today"
    assert "mckenzie" in intent["arguments"]["holding_name"].casefold()

    msg = _write_result_message(
        "create_expense",
        {
            "created": True,
            "expense": {
                "amount": "18.41",
                "description": "Draino",
                "scope": "950 McKenzie Ave — whole property",
                "paid_on": "2026-07-31",
            },
        },
    )
    assert msg.startswith("Logged $18.41")
    assert "Created 950" not in msg


def test_void_message_never_creates_expense(landlord):
    from rentium.rama.service import _verbal_expense_intent
    from rentium.rama.service import _void_expense_intent

    live = {"listings": [{"address": "950 McKenzie Ave", "name": "Room A"}]}
    text = (
        'void the wrong "Jul 31 Maintenance expense for adding new window '
        'screensExpense 950 McKenzie Ave −$125.00 Not yet taken"'
    )
    assert _verbal_expense_intent(landlord, text, live) is None
    void_intent = _void_expense_intent(landlord, text)
    assert void_intent is not None
    assert void_intent["tool"] == "void_ledger_entry"
    assert void_intent["arguments"]["amount"] == "125.00"


def test_practical_phrase_routing():
    from rentium.rama.capabilities import supported_tool_for_request as s

    assert s("has siya signed the lease") == "tenant_lease_status"
    assert s("mark the draino expense as paid") == "mark_ledger_paid"
    assert s("why does it say Not yet taken") == "mark_ledger_paid"
    assert s("void both 125 expenses") == "void_ledger_entry"
    assert s("cancel the viewing for Ishupreet") == "cancel_viewing"
    assert s("have they seen the viewing link") == "viewing_invite_status"
    assert s("reschedule the viewing to tomorrow 4pm") == "reschedule_viewing"


def test_make_viewing_not_hijacked_by_calendar_link(landlord):
    from rentium.rama.capabilities import supported_tool_for_request
    from rentium.rama.service import _dashboard_collection_intent
    from rentium.rama.service import _schedule_viewing_intent

    msg = (
        "Make a viewing for 950 mckenzie garden suite tomorrow at 3 pm for "
        "Ishupreet Sidhu and send her an email for it Ishusidhu.3600@gmail.com"
    )
    assert _dashboard_collection_intent(msg) is None
    assert supported_tool_for_request(msg) == "schedule_viewing"
    intent = _schedule_viewing_intent(msg)
    assert intent is not None
    assert intent["tool"] == "schedule_viewing"
    assert "15:00" in intent["arguments"]["when"]
    assert intent["arguments"]["contact_email"].lower().startswith("ishusidhu")
    assert intent["arguments"]["property_query"].casefold() == "garden suite"
    assert intent["arguments"]["contact_name"] == "Ishupreet Sidhu"


def test_void_both_duplicate_expenses_by_amount(landlord):
    from decimal import Decimal

    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import ExpenseCategory
    from rentium.rama import registry

    holding = _holding(landlord)
    for desc in (
        "Maintenance expense for adding new window screens",
        'void the wrong "Jul 31 Maintenance expense for adding new window screens"',
    ):
        ledger_services.post_expense(
            landlord=landlord,
            amount=Decimal("125.00"),
            category=ExpenseCategory.MAINTENANCE,
            description=desc,
            holding=holding,
            created_by=landlord.user,
        )
    preview = registry.execute(
        "void_ledger_entry",
        {
            "amount": "125.00",
            "description_query": "window screens",
            "reason": "Duplicates",
            "void_all": "yes",
        },
        landlord=landlord,
    )
    assert preview.get("needs_confirm"), preview
    assert preview["preview"]["count"] == 2
    done = registry.execute(
        "void_ledger_entry",
        {
            "amount": "125.00",
            "description_query": "window screens",
            "reason": "Duplicates",
            "void_all": "yes",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("voided") is True
    assert done.get("count") == 2


def test_naming_the_address_resolves_to_the_holding_not_its_only_unit(landlord):
    """The exact regression: "950 McKenzie Ave" in property_query must mean the
    whole property, not the single unit that happens to live inside it."""
    holding = _holding(landlord)
    _unit(landlord, holding)

    preview = _expense(
        landlord, amount="10.00", description="Gas", property_query="950 McKenzie Ave"
    )
    assert preview["preview"]["property"] == "950 McKenzie Ave — whole property"


def test_a_second_unit_no_longer_turns_the_same_request_into_an_error(landlord):
    """Latent fault in the old resolver: with two units the token match
    returned >1 and errored, so the request worked only by luck."""
    holding = _holding(landlord)
    _unit(landlord, holding, "Basement")
    _unit(landlord, holding, "Upstairs")

    preview = _expense(
        landlord, amount="10.00", description="Gas", property_query="950 McKenzie Ave"
    )
    assert "error" not in preview
    assert preview["preview"]["property"] == "950 McKenzie Ave — whole property"


# ------------------------------------------------- the other scopes still work
def test_a_by_room_unit_says_shared(landlord):
    """The old code hardcoded '— shared' for every unit. It is only correct
    for a BY_ROOM unit, where the cost really is shared between the rooms."""
    from rentium.properties.models import PropertyUnit

    holding = _holding(landlord)
    unit = _unit(landlord, holding, "Basement")
    unit.rental_mode = PropertyUnit.RentalMode.BY_ROOM
    unit.save(update_fields=["rental_mode"])

    preview = _expense(
        landlord, amount="20.00", description="Shower knob", property_query="Basement"
    )
    assert preview["preview"]["property"] == "Basement (950 McKenzie Ave) — shared"


def test_a_whole_unit_rental_says_whole_unit_not_shared(landlord):
    """create_work_order distinguishes these two; create_expense hardcoded
    '— shared' regardless of how the unit is actually let."""
    from rentium.properties.models import PropertyUnit

    holding = _holding(landlord)
    unit = _unit(landlord, holding, "Garden Suite")
    unit.rental_mode = PropertyUnit.RentalMode.WHOLE_UNIT
    unit.save(update_fields=["rental_mode"])

    preview = _expense(
        landlord, amount="20.00", description="Filter", property_query="Garden Suite"
    )
    assert preview["preview"]["property"] == "Garden Suite (950 McKenzie Ave) — whole unit"


def test_a_portfolio_wide_expense_needs_no_scope(landlord):
    from rentium.ledger.models import LedgerEntry

    out = _expense(
        landlord, amount="99.00", description="Accounting software", confirm="yes"
    )
    entry = LedgerEntry.objects.get(pk=out["expense"]["id"])
    assert entry.property_id is None and entry.holding_id is None


def test_an_ambiguous_name_asks_instead_of_picking(landlord):
    holding = _holding(landlord)
    _unit(landlord, holding, "Suite A")
    _unit(landlord, holding, "Suite B")

    preview = _expense(
        landlord, amount="10.00", description="Gas", property_query="Suite"
    )
    assert "error" in preview
    assert preview["candidates"]


# ------------------------------------------------------- independent batching
def test_two_expenses_on_one_holding_do_not_share_an_item_key(landlord):
    """The latent skip-cascade: both steps carried item_key
    'entity:950 McKenzie Ave', so a failure in the first would have silently
    SKIPPED the second as a 'chained operation on one property'."""
    from rentium.rama.plan_runner import save_batch

    _holding(landlord)
    specs = [
        {
            "kind": "single",
            "tool": "create_expense",
            "arguments": {
                "amount": "31.45",
                "description": "Mulch",
                "holding_name": "950 McKenzie Ave",
            },
            "target": "950 McKenzie Ave",
        },
        {
            "kind": "single",
            "tool": "create_expense",
            "arguments": {
                "amount": "10.00",
                "description": "Gas",
                "holding_name": "950 McKenzie Ave",
            },
            "target": "950 McKenzie Ave",
        },
    ]
    plan = save_batch(landlord, __import__("uuid").uuid4(), specs)
    keys = [s.item_key for s in plan.steps.order_by("order")]
    assert len(keys) == 2
    assert keys[0] != keys[1]


def test_chained_operations_on_one_property_still_share_an_item_key(landlord):
    """The exemption must not break what item_key was built for."""
    from rentium.rama.plan_runner import save_batch

    specs = [
        {
            "kind": "single",
            "tool": "update_property",
            "arguments": {"property_query": "Room A", "name": "Room A2"},
            "target": "Room A",
        },
        {
            "kind": "single",
            "tool": "assign_property_to_group",
            "arguments": {"property_query": "Room A", "group_name": "Maple"},
            "target": "Room A",
        },
    ]
    plan = save_batch(landlord, __import__("uuid").uuid4(), specs)
    keys = [s.item_key for s in plan.steps.order_by("order")]
    assert keys[0] == keys[1]


def test_receipt_correction_is_not_verbal_expense(landlord):
    """OCR misread $1000 gift card; landlord says real total + already logged → not create_expense."""
    from rentium.rama.service import _looks_like_receipt_followup
    from rentium.rama.service import _verbal_expense_intent
    from rentium.rama.service import _wants_link_existing_expense
    from rentium.rama.service import _amount_from_message

    live = {
        "listings": [
            {"address": "950 McKenzie Ave", "name": "Room A"},
        ]
    }
    text = (
        "No it's $13.41 (the $1000 figure was just about a gift card purchase) "
        "and the draino purchase was for 950 Mckenzie Ave u should know. "
        "You should know the expense is logged so just store this as the "
        "receipt/document"
    )
    assert _looks_like_receipt_followup(text) is True
    assert _wants_link_existing_expense(text) is True
    assert _amount_from_message(text) == "13.41"
    assert _verbal_expense_intent(landlord, text, live) is None


def test_receipt_followup_catalogs_and_links_existing_expense(landlord):
    """Pending unscoped receipt + correction + 'expense is logged' → link, no second post."""
    import uuid
    from decimal import Decimal
    from datetime import date

    from django.core.files.uploadedfile import SimpleUploadedFile

    from rentium.ledger.models import EntryType, ExpenseCategory, LedgerEntry
    from rentium.rama.document_services import ingest_document
    from rentium.rama.models import RamaAudit, RamaDocument
    from rentium.rama.service import _amount_from_message
    from rentium.rama.service import _looks_like_receipt_followup
    from rentium.rama.service import _pending_unscoped_document_id
    from rentium.rama.service import _verbal_expense_intent
    from rentium.rama.service import _wants_link_existing_expense
    from rentium.rama import registry

    holding = _holding(landlord)
    # Existing chat-logged expense (no receipt yet).
    entry = LedgerEntry.objects.create(
        landlord=landlord,
        holding=holding,
        entry_type=EntryType.EXPENSE,
        amount=Decimal("13.41"),
        description="Draino for 950 McKenzie house",
        category=ExpenseCategory.SUPPLIES,
        effective_date=date.today(),
    )
    upload = SimpleUploadedFile(
        "draino.jpg", b"%PDF-draino-receipt-bytes", content_type="application/pdf"
    )
    document, _ = ingest_document(landlord=landlord, upload=upload)
    document.kind = RamaDocument.Kind.EXPENSE
    document.title = "Expense Invoice"
    document.amount = Decimal("1000.00")  # OCR misread gift card
    document.expense_category = ExpenseCategory.SUPPLIES
    document.payment_state = RamaDocument.PaymentState.UNKNOWN
    document.status = RamaDocument.Status.NEEDS_REVIEW
    document.save()

    conversation = uuid.uuid4()
    RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=conversation,
        kind=RamaAudit.Kind.TOOL_CALL,
        content={
            "tool": "catalog_business_document",
            "arguments": {},
            "result": {
                "prepared": True,
                "document_id": str(document.pk),
                "needs_scope": True,
            },
        },
    )

    text = (
        "No it's $13.41 (the $1000 figure was just about a gift card purchase) "
        "and the draino purchase was for 950 Mckenzie Ave. "
        "The expense is logged so just store this as the receipt/document"
    )
    live = {"listings": [{"address": "950 McKenzie Ave", "name": "Room A"}]}
    assert _verbal_expense_intent(landlord, text, live) is None
    assert _looks_like_receipt_followup(text)
    assert _wants_link_existing_expense(text)
    assert _amount_from_message(text) == "13.41"
    assert _pending_unscoped_document_id(landlord, conversation) == str(document.pk)

    from rentium.rama.service import _apply_document_amount_correction
    from rentium.rama.service import _address_scope_from_message

    _apply_document_amount_correction(landlord, str(document.pk), "13.41")
    document.refresh_from_db()
    assert document.amount == Decimal("13.41")

    scope = _address_scope_from_message(text, live)
    assert "mckenzie" in scope.casefold()

    catalogued = registry.execute(
        "catalog_business_document",
        {
            "document_id": str(document.pk),
            "scope_query": scope,
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert catalogued.get("catalogued") or catalogued.get("updated"), catalogued

    linked = registry.execute(
        "file_business_document",
        {
            "document_id": str(document.pk),
            "amount": "13.41",
            "duplicate_resolution": "auto_link",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert linked.get("linked_existing") or linked.get("filed"), linked
    document.refresh_from_db()
    assert document.status == RamaDocument.Status.FILED
    assert document.ledger_entry_id == entry.pk
    assert (
        LedgerEntry.objects.not_voided()
        .filter(landlord=landlord, entry_type=EntryType.EXPENSE)
        .count()
        == 1
    )
