"""A denial must be backed by a read of somewhere the thing could have been.

The August 2026 case: "Short answer: No — I don't see any discounts recorded for
this month" while a $1,600 DISCOUNT sat in leases.RentAdjustment, which the
manifest did not expose at all.

Half of these tests are false-positive tests. A guard that rewrites replies is
only safe if it stays quiet on ordinary ones — the last pass shipped guards that
condemned 7 of 9 correct read-only answers, so the bar here is that a legitimate
"no" backed by an actual read passes through untouched.
"""

from __future__ import annotations

import pytest

from rentium.rama.coverage import concept_index
from rentium.rama.coverage import denial_sentences
from rentium.rama.coverage import look_here_first
from rentium.rama.coverage import primary_index
from rentium.rama.coverage import unchecked_denials

ASKED_DISCOUNT = "Did I provide anyone discount this month?"


# --------------------------------------------------------------- the index
def test_discount_resolves_to_rent_adjustment():
    """The word the landlord used, from an enum choice nobody labelled
    'discount' — RentAdjustment.AdjustmentType.DISCOUNT."""
    assert "rent_adjustment" in concept_index()["discount"]


def test_proration_and_increase_resolve_too():
    index = concept_index()
    assert "rent_adjustment" in index["proration"]
    assert "rent_adjustment" in index["increase"]


def test_generic_words_are_not_indexed():
    """A word owned by every entity points nowhere useful."""
    index = concept_index()
    for word in ("date", "amount", "status", "name", "total"):
        assert word not in index, f"{word!r} is too generic to index"


def test_no_denial_trigger_word_is_also_a_concept():
    """The words that make a sentence a denial cannot be things being denied.

    Caught in review: property_unit declares a field labelled "Layout
    recorded", so "no discounts recorded" indexed "recorded" → property_unit
    and the guard flagged a denial about its own trigger word. Every ordinary
    reply containing "recorded" would have cost a wasted round.
    """
    from rentium.rama.coverage import _DENIAL_VOCAB

    collisions = sorted(set(concept_index()) & _DENIAL_VOCAB)
    assert not collisions, (
        f"these words both trigger the guard and name a concept: {collisions}"
    )


def test_index_covers_every_entity():
    owned = {key for keys in concept_index().values() for key in keys}
    from rentium.rama.manifest import MANIFEST

    assert owned == set(MANIFEST), "every entity must be reachable by some word"


# ----------------------------------------------------------- denial detection
@pytest.mark.parametrize(
    "text",
    [
        "Short answer: No — I don't see any discounts recorded for this month.",
        "There are no discounts on file for August.",
        "No discount recorded against that lease.",
        "A query for discounts returned 0 rows.",
        "Nothing matching a discount was found.",
    ],
)
def test_denials_are_recognised(text):
    assert denial_sentences(text), f"should read as a denial: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "The August discount brought Naveen's rent to $400.00.",
        "I can prepare a discount preview — tell me the lease and the amount.",
        "Two leases have a discount recorded: RMT652523-C281 and RMT415536-0617.",
        "Rent charges for August total $2,750.00.",
        "Room C is $850.00/month.",
    ],
)
def test_ordinary_answers_are_not_denials(text):
    assert denial_sentences(text) == [], f"should NOT read as a denial: {text!r}"


# ------------------------------------------------------- the guard, in context
def test_the_august_denial_is_caught():
    """The verbatim reply that started this."""
    reply = (
        "Short answer: No — I don't see any discounts recorded for this month "
        "(Aug 2026).\n"
        "What I checked:\n"
        "• Ledger (due_date in Aug 2026) shows only Rent charges and Deposit "
        "charges.\n"
        "• A query for negative amounts in Aug 2026 returned 0 rows."
    )
    flagged = unchecked_denials(
        reply, {"ledger_entry"}, landlord_message=ASKED_DISCOUNT,
    )
    assert "discount" in flagged
    assert flagged["discount"] == frozenset({"rent_adjustment"})


def test_the_same_denial_passes_once_the_right_table_was_read():
    """This is the whole point: the sentence is fine, the evidence was missing."""
    reply = "Short answer: No — I don't see any discounts recorded for Aug 2026."
    assert unchecked_denials(
        reply,
        {"ledger_entry", "rent_adjustment"},
        landlord_message=ASKED_DISCOUNT,
    ) == {}


def test_reading_only_one_of_two_homes_is_not_enough():
    """The actual transcript. "discount" lives in ledger_entry (entry_type
    CREDIT, "Credit / Discount") AND rent_adjustment. RAMA read the ledger,
    which was genuinely empty, and concluded there were none. Ruling something
    out needs every place checked; confirming it needs only one."""
    reply = "Short answer: No — I don't see any discounts recorded for Aug 2026."
    flagged = unchecked_denials(
        reply, {"ledger_entry"}, landlord_message=ASKED_DISCOUNT,
    )
    assert flagged["discount"] == frozenset({"rent_adjustment"})


def test_a_denial_about_nothing_in_the_manifest_is_left_alone():
    """RAMA has no opinion about concepts it was never asked to model."""
    reply = "No, there are no parking permits recorded for that unit."
    assert unchecked_denials(
        reply, {"lease"}, landlord_message="any parking permits for that unit?",
    ) == {}


def test_a_positive_answer_is_never_flagged():
    reply = (
        "Naveen received a discount: his August rent was adjusted to $400.00 "
        "on lease RMT652523-C281."
    )
    assert unchecked_denials(
        reply, set(), landlord_message=ASKED_DISCOUNT,
    ) == {}


def test_an_incidental_noun_in_the_answer_never_fires():
    """Pinned from a live run. Asked about August deposits, RAMA answered
    correctly — and the words "Room C" and "Garden Suite" in its own reply
    matched incidental label words, so the guard sent it to read `inventory`
    and it came back with a list of beds and nightstands instead of the
    deposits. A guard that can replace a right answer with a wrong one is worse
    than no guard.
    """
    asked = (
        "Have i received deposits from everyone who was to move in from "
        "august? and if so, how much?"
    )
    reply = (
        "Short answer: No — not everyone.\n"
        "RMT685028-93F5 — Room C — deposit charged 425.00, settled 100.00.\n"
        "RMT698948-2EA3 — Garden Suite — no deposit charge recorded."
    )
    assert unchecked_denials(reply, {"ledger_entry", "lease"}, landlord_message=asked) == {}


def test_only_a_primary_owner_can_fire():
    """`deposit` is a field label on several entities and the name of none, so
    reading any of them is a defensible partial answer. `discount` is what
    rent_adjustment IS."""
    assert "deposit" not in primary_index()
    assert primary_index()["discount"] == frozenset({"rent_adjustment"})


def test_a_denial_about_something_never_asked_is_left_alone():
    """The claim has to be about the question. A noun that only appears in the
    answer is an aside, not the thing being ruled out."""
    reply = "No discounts are recorded for that lease."
    assert unchecked_denials(
        reply, {"ledger_entry"}, landlord_message="what is Room C's rent?",
    ) == {}


def test_instruction_keeps_the_model_on_the_original_question():
    text = look_here_first({"discount": frozenset({"rent_adjustment"})})
    assert "ORIGINAL QUESTION" in text
    assert "new topic" in text


def test_instruction_names_where_to_look():
    text = look_here_first({"discount": frozenset({"rent_adjustment"})})
    assert "rent_adjustment" in text
    assert "discount" in text
    assert "read" in text.casefold()


def test_guard_survives_empty_and_none():
    assert unchecked_denials("", set(), landlord_message="x") == {}
    assert unchecked_denials(None, set(), landlord_message="x") == {}
    assert unchecked_denials("No discounts.", set(), landlord_message="") == {}
    assert denial_sentences(None) == []
