"""
Knowing what a sentence is ABOUT, instead of guessing from the numbers in it.

    "instead of adding each edge case, can it not be smart enough to do these
     things"

The transcript that forced this:

    > Okay I received amanats deposit. It's one $400 payment that covers her
      both deposits

    Ready to store this as a business document for the physical property:
    • Address: 950 McKenzie Ave …

    > No wtf

One cause, several symptoms. `_amount_from_message` returns any dollar figure
in any sentence, and `can_catalog` took a bare amount as evidence that the
landlord was correcting a receipt. With an unfiled document still sitting in
the conversation — they live 30 minutes — every message that mentioned money
became a document turn. The same stale plan then absorbed a "Yes" that had been
given to a proposal about deposit charges, and answered "Stored at the
physical-property level."

Two fixes, both structural rather than another special case:

  * a number is not a subject. The amount is gone from the routing condition,
    so the regex path cannot be hijacked even with no model available.
  * above that, the model reads the sentence and says what it is about. The
    deterministic routers are gated on the answer; they still do all the work.

Nothing here trusts the model with an outcome. It picks from five words; Python
decides what runs, previews it, and asks.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from rentium.rama.providers.base import ProviderError, Turn

pytestmark = pytest.mark.django_db

CONVERSATION = uuid.uuid4()


@pytest.fixture
def configured(landlord, settings):
    from rentium.rama.models import RamaPreferences

    settings.ANTHROPIC_API_KEY = "test-key"
    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.provider = "anthropic"
    prefs.model = "claude-haiku-4-5"
    prefs.save()
    return landlord


class _Fake:
    def __init__(self, text="", raises=None):
        self.text = text
        self.raises = raises

    def complete(self, **kwargs):
        if self.raises:
            raise self.raises
        return Turn(text=self.text)


def _subject(landlord, message, answer):
    from rentium.rama.service import _turn_subject

    with patch("rentium.rama.providers.get_provider", return_value=_Fake(answer)):
        return _turn_subject(landlord, CONVERSATION, message)


# ============================================ a number is not a subject
def test_a_bare_amount_no_longer_routes_to_documents():
    """The whole hijack, at the level it actually happened. This is the
    deterministic half — it holds with no model available at all."""
    from rentium.rama.service import _amount_from_message, _looks_like_receipt_followup

    said = "Okay I received amanats deposit. It's one $400 payment that covers her both deposits"

    # The amount is still extracted — other things legitimately use it...
    assert _amount_from_message(said) == "400"
    # ...but it is no longer, on its own, a reason to think this is a receipt.
    assert _looks_like_receipt_followup(said) is False


def test_the_routing_condition_does_not_mention_the_amount():
    """Guards the fix itself. `amount_correction` back in this condition would
    silently restore the hijack, and only a transcript would catch it."""
    import inspect

    from rentium.rama import service

    source = inspect.getsource(service._run_turn_unlocked)
    start = source.index("can_catalog = (")
    condition = source[start : source.index(")\n\n", start)]
    assert "amount_correction" not in condition


# =================================================== reading the sentence
@pytest.mark.parametrize(
    "said,answer",
    [
        ("Okay I received amanats deposit. It's one $400 payment", "money_received"),
        ("Rent came in for Room D", "money_received"),
        ("I bought a rat trap for McKenzie house, $67.19", "money_spent"),
        ("No it's 67.19 for the rat trap", "filing_a_document"),
        ("Has amanat signed the lease", "lease_admin"),
        ("Why didn't u notify me", "something_else"),
    ],
)
def test_the_subject_comes_back(configured, said, answer):
    assert _subject(configured, said, answer) == answer


def test_an_unreadable_answer_is_no_subject(configured):
    """Same contract as everywhere else: off-contract means no opinion, and
    the tightened regex path stands."""
    assert _subject(configured, "anything", "probably a receipt?") is None


def test_an_outage_is_no_subject(configured):
    from rentium.rama.service import _turn_subject

    with patch(
        "rentium.rama.providers.get_provider",
        return_value=_Fake(raises=ProviderError("down")),
    ):
        assert _turn_subject(configured, CONVERSATION, "anything") is None


# ============================== a stale plan must not absorb an unrelated yes
def _document_plan(landlord):
    from rentium.rama.plan_runner import save_single

    return save_single(
        landlord,
        CONVERSATION,
        "catalog_business_document",
        {"document_id": str(uuid.uuid4()), "scope_query": "950 McKenzie Ave"},
    )


def test_a_document_plan_is_recognised_as_one(landlord):
    from rentium.rama.service import _plan_subject

    assert _plan_subject(_document_plan(landlord)) == "filing_a_document"


def test_a_payment_plan_is_recognised_as_one(landlord):
    from rentium.rama.plan_runner import save_single
    from rentium.rama.service import _plan_subject

    plan = save_single(
        landlord, CONVERSATION, "record_payment", {"amount": "400"},
    )
    assert _plan_subject(plan) == "money_received"


def test_a_plan_of_no_known_subject_is_left_alone(landlord):
    """Only the money/document plans are arbitrated this way. A lease or
    property plan must not be discarded because somebody mentioned a receipt."""
    from rentium.rama.plan_runner import save_single
    from rentium.rama.service import _plan_subject

    plan = save_single(
        landlord,
        CONVERSATION,
        "create_property",
        {"name": "Room Z", "address": "950 McKenzie Ave", "city": "Victoria"},
    )
    assert _plan_subject(plan) is None


def test_yes_and_no_are_not_a_change_of_subject(configured):
    """The landlord answering the plan in front of them must never be read as
    moving on from it — that would discard the plan they are confirming."""
    for word in ("yes", "no", "yep do it"):
        assert _subject(configured, word, "answering_a_question") == (
            "answering_a_question"
        )
