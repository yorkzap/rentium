"""
Letting the model read the sentence, without letting it decide the outcome.

    "It's left the bank", "It's been paid", "It's been charged" etc, we cannot
     come up with each wording or pattern for this. RAMA should be smart enough
     to know what it means. That's the whole point of an LLM. […] I like the
     guardrails but for decisions like this, we need to do something while
     being safe."

The regex had already missed the most ordinary phrasing there is, and the next
alternation would have missed the one after that. So interpretation moves to
the model — and everything that makes an interpretation dangerous is closed off
in Python around it. These tests are that boundary, stated as behaviour:

  * the answer must come from the caller's closed set, or it is thrown away
  * no key / no provider / an exception is "no opinion", never a crash
  * an interpretation reaches no tools, so it can never become a write
  * abstaining is a real answer and is not overridden by a pattern guess

Everything downstream is unchanged: the figure is read from the database, the
preview is built by Python, and the landlord still says yes.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from rentium.rama.interpret import classify
from rentium.rama.providers.base import ProviderError, Turn

pytestmark = pytest.mark.django_db


@pytest.fixture
def configured(landlord, settings):
    """A landlord whose RAMA is switched on with a usable key."""
    from rentium.rama.models import RamaPreferences

    settings.ANTHROPIC_API_KEY = "test-key"
    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.provider = "anthropic"
    prefs.model = "claude-haiku-4-5"
    prefs.save()
    return landlord


class _Fake:
    """A provider that answers with whatever the test wants, and records how
    it was called — the tool list is the thing worth asserting on."""

    def __init__(self, text="", raises=None):
        self.text = text
        self.raises = raises
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return Turn(text=self.text)


def _ask(landlord, fake, message="yes, and its left the bank"):
    with patch("rentium.rama.providers.get_provider", return_value=fake):
        return classify(
            landlord,
            question="Has the money already left their bank?",
            message=message,
            options=("paid", "unpaid", "unclear"),
        )


# ================================================= it does the useful thing
@pytest.mark.parametrize("said", ["paid", "PAID", " paid\n", "paid."])
def test_an_option_word_comes_back_however_it_is_dressed(configured, said):
    assert _ask(configured, _Fake(said)) == "paid"


def test_the_callers_spelling_is_what_comes_back(configured):
    """Options are matched case-insensitively but returned verbatim, so a
    caller comparing against its own constants is never surprised."""
    with patch("rentium.rama.providers.get_provider", return_value=_Fake("PAID")):
        assert classify(
            configured,
            question="q",
            message="it's gone through",
            options=("Paid", "Unpaid"),
        ) == "Paid"


# ============================================ what it refuses to hand back
def test_an_answer_outside_the_options_is_discarded(configured):
    """The whole safety property. A model that answers something else has not
    made a decision RAMA is willing to act on."""
    assert _ask(configured, _Fake("probably paid, but check with them")) is None
    assert _ask(configured, _Fake("settled")) is None
    assert _ask(configured, _Fake("")) is None


def test_a_number_never_comes_back(configured):
    """It classifies; it never supplies. A figure in the reply is not a figure
    RAMA will use — amounts are read from the database, always."""
    assert _ask(configured, _Fake("$37.16")) is None
    assert _ask(configured, _Fake("paid $37.16")) is None


def test_a_sentence_containing_the_word_is_not_trusted(configured):
    """"one word only" was the instruction. A model that wrote a paragraph did
    not follow the contract, and the option buried in it is not evidence it
    meant that option."""
    assert _ask(configured, _Fake("I think the answer is paid")) is None


# ================================================ it degrades, never breaks
def test_no_api_key_is_no_opinion(landlord, settings):
    settings.ANTHROPIC_API_KEY = ""
    assert (
        classify(
            landlord, question="q", message="it's left the bank", options=("paid",),
        )
        is None
    )


def test_a_provider_outage_is_no_opinion(configured):
    assert _ask(configured, _Fake(raises=ProviderError("upstream is down"))) is None


def test_an_unexpected_exception_is_no_opinion(configured):
    """A classifier that raises would take the landlord's whole turn with it."""
    assert _ask(configured, _Fake(raises=RuntimeError("boom"))) is None


def test_it_can_be_switched_off_entirely(configured, settings):
    settings.RAMA_SEMANTIC_INTERPRETATION = False
    fake = _Fake("paid")
    assert _ask(configured, fake) is None
    assert fake.calls == []  # and costs nothing when off


def test_an_empty_message_asks_nobody(configured):
    fake = _Fake("paid")
    assert _ask(configured, fake, message="   ") is None
    assert fake.calls == []


# ====================================================== it has no reach
def test_an_interpretation_is_given_no_tools(configured):
    """The hard guarantee. With an empty tool list, an interpretation cannot
    become a write no matter what the model decides it wants to do."""
    fake = _Fake("paid")
    _ask(configured, fake)
    assert fake.calls[0]["tools"] == []


def test_the_decision_is_on_the_record(configured):
    """A reading the landlord disputes has to be readable afterwards."""
    import uuid

    from rentium.rama.models import RamaAudit

    conversation = uuid.uuid4()
    with patch("rentium.rama.providers.get_provider", return_value=_Fake("paid")):
        classify(
            configured,
            question="Has the money already left their bank?",
            message="yes, and its left the bank",
            options=("paid", "unpaid", "unclear"),
            conversation_id=conversation,
        )
    row = (
        RamaAudit.objects.filter(landlord=configured, conversation_id=conversation)
        .order_by("-created_at")
        .first()
    )
    assert row.content["tool"] == "interpret.classify"
    assert row.content["result"]["chosen"] == "paid"


# ======================================== the case the landlord complained of
@pytest.mark.parametrize(
    "said",
    [
        "yes, and its left the bank",
        "it's been charged",
        "the card's been hit already",
        "that cleared on Tuesday",
        "my account is lighter, yes",
        "aye it's away",
    ],
)
def test_any_phrasing_at_all_reaches_the_decision(configured, said):
    """Not a test that the model is right — a test that RAMA now ASKS. Every
    one of these was a turn the regex dropped on the floor."""
    fake = _Fake("paid")
    with patch("rentium.rama.providers.get_provider", return_value=fake):
        from rentium.rama.service import _expense_bank_status

        assert _expense_bank_status(configured, said) == "paid"


def test_the_patterns_still_catch_it_when_the_model_cannot_be_reached(
    configured,
):
    """The regex is the floor to degrade to, not the ceiling on understanding.
    With the provider down, RAMA is less smart and still correct."""
    from rentium.rama.service import _expense_bank_status

    fake = _Fake(raises=ProviderError("down"))
    with patch("rentium.rama.providers.get_provider", return_value=fake):
        assert _expense_bank_status(configured, "yes, and its left the bank") == "paid"
        assert _expense_bank_status(configured, "no, still unpaid") == "unpaid"


def test_an_abstention_is_not_overruled_by_the_patterns(configured):
    """"unclear" is the model having read the sentence and judged it not an
    answer. Letting the regex then guess would be the back door — RAMA asks a
    plain question instead."""
    from rentium.rama.service import _expense_bank_status

    fake = _Fake("unclear")
    with patch("rentium.rama.providers.get_provider", return_value=fake):
        # Contains "paid", which the pattern list would have seized on.
        assert _expense_bank_status(configured, "I paid the tenant back") is None
