"""
RAMA can move an expense to the scope it belongs to.

Written from a real transcript. A $19.78 hot water knob was recorded against
Room C, then had to move to the address because the shower it fixed serves
Rooms C, D and F. RAMA had create_expense and nothing else — no void tool, no
reallocation helper — so the "correction" was improvised: a fresh expense at the
new scope, the old one voided out of band. Three ledger rows for one repair,
nothing linking them, and no reason recorded.

The lesson these tests encode is not about arithmetic. It is that when RAMA has
adjacent primitives but not the actual capability, it composes them — so the
capability has to exist as one named, confirm-first operation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from rentium.rama import registry

pytestmark = pytest.mark.django_db


def _holding(landlord, name="950 McKenzie Ave"):
    from rentium.properties.models import PropertyHolding

    return PropertyHolding.objects.create(
        landlord=landlord, name=name, address=name, city="Victoria"
    )


def test_reallocate_phrases_map_to_tool():
    from rentium.rama.capabilities import supported_tool_for_request

    assert (
        supported_tool_for_request(
            "that expense should be on the address not the room"
        )
        == "reallocate_expense"
    )
    assert (
        supported_tool_for_request("reallocate the expense to the house")
        == "reallocate_expense"
    )
    assert (
        supported_tool_for_request("move the cost off the room")
        == "reallocate_expense"
    )


def _room(landlord, holding, name="Room C"):
    from rentium.properties.models import Property

    return Property.objects.create(
        landlord=landlord,
        holding=holding,
        name=name,
        address=holding.address,
        city="Victoria",
        province="BC",
        postal_code="V8V 1V1",
        property_category=Property.PropertyCategory.ROOM,
    )


def _knob(landlord, prop, description="Hot water knob replacement"):
    from rentium.ledger import services

    entry, _ = services.post_expense(
        landlord=landlord,
        amount="19.78",
        category="MAINTENANCE",
        description=description,
        property=prop,
    )
    return entry


def _run(landlord, **kwargs):
    return registry.execute("reallocate_expense", kwargs, landlord=landlord)


SHARED = "Shared-space repair: the shower serves Rooms C, D and F"


def test_the_tool_is_registered(landlord):
    """The defect, stated as a test: grepping RAMA for a void or reallocation
    tool returned nothing, so it improvised instead of saying it could not."""
    assert "reallocate_expense" in registry.REGISTRY


def test_it_previews_both_scopes_before_touching_money(landlord):
    holding = _holding(landlord)
    room = _room(landlord, holding)
    _knob(landlord, room)

    out = _run(
        landlord,
        expense_query="hot water knob",
        holding_name="950 McKenzie Ave",
        reason=SHARED,
    )

    assert out["needs_confirm"] is True
    assert out["preview"]["from"] == "Room C"
    assert out["preview"]["to"] == "950 McKenzie Ave — whole property"
    assert out["preview"]["amount"] == "19.78"


def test_confirming_leaves_one_live_line_linked_to_what_it_replaced(landlord):
    from rentium.ledger.models import EntryType, LedgerEntry

    holding = _holding(landlord)
    room = _room(landlord, holding)
    original = _knob(landlord, room)

    out = _run(
        landlord,
        expense_query="hot water knob",
        holding_name="950 McKenzie Ave",
        reason=SHARED,
        confirm="yes",
    )

    assert out["created"] is True
    assert out["expense"]["scope"] == "950 McKenzie Ave — whole property"
    assert out["expense"]["previous_scope"] == "Room C"
    assert out["expense"]["replaces"] == str(original.pk)

    live = LedgerEntry.objects.filter(
        landlord=landlord, entry_type=EntryType.EXPENSE
    ).not_voided()
    assert live.count() == 1
    assert live.first().holding_id == holding.pk
    assert live.first().property_id is None


def test_an_ambiguous_expense_returns_candidates_rather_than_a_guess(landlord):
    """Voiding the wrong row is not something a second correction tidies up."""
    holding = _holding(landlord)
    room = _room(landlord, holding)
    _knob(landlord, room, description="Hot water knob replacement — upstairs")
    _knob(landlord, room, description="Hot water knob replacement — downstairs")

    out = _run(
        landlord,
        expense_query="hot water knob",
        holding_name="950 McKenzie Ave",
        reason=SHARED,
    )

    assert "error" in out
    assert len(out["candidates"]) == 2
    assert "created" not in out


def test_a_reason_is_required(landlord):
    holding = _holding(landlord)
    room = _room(landlord, holding)
    _knob(landlord, room)

    out = _run(
        landlord, expense_query="hot water knob", holding_name="950 McKenzie Ave"
    )
    assert "audit trail" in out["error"]


def test_a_destination_is_required(landlord):
    holding = _holding(landlord)
    room = _room(landlord, holding)
    _knob(landlord, room)

    out = _run(landlord, expense_query="hot water knob", reason=SHARED)
    assert "Where should it go" in out["error"]


def test_replaying_the_same_reallocation_does_not_post_a_third_row(landlord):
    from rentium.ledger.models import EntryType, LedgerEntry

    holding = _holding(landlord)
    room = _room(landlord, holding)
    _knob(landlord, room)
    args = dict(
        expense_query="hot water knob",
        holding_name="950 McKenzie Ave",
        reason=SHARED,
        confirm="yes",
    )
    _run(landlord, **args)

    # The original no longer resolves (it is voided) and the replacement is
    # already where it was asked to go, so a replay cannot double-post.
    again = _run(landlord, **args)
    assert "error" in again

    live = LedgerEntry.objects.filter(
        landlord=landlord, entry_type=EntryType.EXPENSE
    ).not_voided()
    assert live.count() == 1
    assert sum(e.amount for e in live) == Decimal("19.78")


# ------------------------------------------------- the improvisation loophole
# The capability-gap register only fired when RAMA had NOTHING. Here it had
# adjacent primitives — create_expense existed — so it composed them into a
# correction that left three unlinked rows. Recognising the intent
# deterministically is what routes it to the one named operation instead.
@pytest.mark.parametrize(
    "phrasing",
    [
        "That repair belongs to the whole house, not Room C.",
        "Move the $19.78 expense to the address.",
        "The shower expense is booked to the wrong room.",
        "Can you reallocate that cost to 950 McKenzie Ave?",
        "That bill shouldn't be on Room C.",
    ],
)
def test_a_correction_shaped_ask_routes_to_the_tool(phrasing):
    from rentium.rama.capabilities import supported_tool_for_request

    assert supported_tool_for_request(phrasing) == "reallocate_expense"


def test_logging_a_gap_for_it_is_refused_now_that_it_exists(landlord):
    """The register must not accumulate a backlog item for a shipped feature."""
    out = registry.execute(
        "log_capability_gap",
        {"request": "Move the shower expense to the address, not Room C."},
        landlord=landlord,
    )
    assert out["logged"] is False
    assert out["tool"] == "reallocate_expense"


def test_recording_a_new_expense_is_not_mistaken_for_a_correction(landlord):
    """The recognizer must not hijack an ordinary 'record this cost' request."""
    from rentium.rama.capabilities import supported_tool_for_request

    assert supported_tool_for_request("Record a $31.45 expense for mulch") is None
    assert supported_tool_for_request("Add an expense for the new dryer") is None
