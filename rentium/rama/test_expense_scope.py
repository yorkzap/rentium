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
