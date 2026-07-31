"""
A promise is not an answer.

THE INCIDENT
------------
    landlord:  its done already i thought
    RAMA:      I'll verify the deposit payment is recorded correctly.
               Checking the ledger now.
    landlord:  ok and? if you have something to say, tell me without me
               having to ask u each time
    RAMA:      [the answer]
    landlord:  No I thought $100 has entered the ledger..
    RAMA:      I'll re-check the ledger for Room C to confirm whether the
               $100 payment is recorded.
    landlord:  and? why do umake me guess, and not answer after cheecking

Twice in one conversation the turn ended on an announcement. The engine had
the tools to answer both times.

The structural cause is one line: `if not turn.tool_calls: break` treats ANY
tool-call-free turn as a finished answer. The two existing post-hoc guards
(_looks_like_confirmation_request, claims_completed_write) both catch FALSE
CLAIMS OF ACTION — neither catches the ABSENCE OF AN ANSWER.
"""

from __future__ import annotations

import uuid
from unittest import mock

import pytest

from rentium.rama.models import RamaAudit, RamaPreferences
from rentium.rama.providers import Turn
from rentium.rama.service import promises_without_delivering as promises
from rentium.rama.service import run_turn

pytestmark = pytest.mark.django_db


class ScriptedProvider:
    """Plays back a fixed sequence of Turns and records what it was sent."""

    name = "scripted"
    api_key_setting = "ANTHROPIC_API_KEY"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, *, model, system, messages, tools, api_key: str = ""):
        self.requests.append({"system": system, "messages": list(messages)})
        if not self.turns:
            return Turn(text="")
        return self.turns.pop(0)


def _enable(landlord):
    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.provider = "xai"
    prefs.api_key = "xai-test-key"
    prefs.save()


def _turn(landlord, message, provider):
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        return run_turn(landlord, message, uuid.uuid4())


# ------------------------------------------------- what counts as a stall
@pytest.mark.parametrize(
    "reply",
    [
        # The two actual sentences.
        "I'll verify the deposit payment is recorded correctly. Checking the ledger now.",
        "I'll re-check the ledger for Room C to confirm whether the $100 payment is recorded.",
        "Let me check the ledger for you.",
        "I'm going to look at the charge schedule.",
        "Checking that now.",
        "I will confirm the balance on that charge.",
        "Let me pull up the lease.",
        "I'll take a look at what's outstanding.",
    ],
)
def test_a_stall_is_detected(reply):
    assert promises(reply, []) is True


# ------------------------------------ what must NOT count (the hard part)
@pytest.mark.parametrize(
    "reply",
    [
        # Already delivered — the word appears inside a real answer.
        "The $100 is recorded: a PAYMENT on July 28 against the Room C deposit.",
        "Checked — $325.00 is still outstanding on that deposit charge.",
        # A genuine future commitment, not a stall.
        "I'll check again tomorrow once the e-transfer clears.",
        "I'll verify it next week when the statement arrives.",
        "I'll look at it after they send the forwarding address.",
        # An offer, not a stall.
        "Shall I check the ledger for you?",
        "Would you like me to verify that?",
        # Plain reads that merely contain a trigger word.
        "Room C is occupied and the lease is under review by the tenant.",
        "The inspection checklist is complete.",
    ],
)
def test_an_answer_is_not_a_stall(reply):
    assert promises(reply, []) is False


def test_a_stall_is_judged_per_sentence():
    """A promise in one sentence is not excused by an answer in another —
    the landlord still got told to wait for something already known."""
    assert promises("You owe $325. I'll go and check the rest now.", []) is True


# --------------------------------------------------- the continuation round
def test_a_stalled_turn_is_pushed_to_answer(landlord):
    """The fix: one more round, with an instruction to finish the thought.
    The landlord sees the answer, not the announcement."""
    _enable(landlord)
    provider = ScriptedProvider(
        [
            Turn(text="I'll verify the deposit payment is recorded. Checking now."),
            Turn(text="The $100 is recorded — $325.00 still owing on the deposit."),
        ]
    )
    result = _turn(landlord, "is the $100 in the ledger?", provider)

    assert result.reply == "The $100 is recorded — $325.00 still owing on the deposit."
    assert len(provider.requests) == 2, "the model was not asked to finish"


def test_the_continuation_tells_the_model_what_to_do(landlord):
    """A nudge that says "be helpful" does not survive a weak model. It has to
    name the shape of the answer: result AND implication, in this turn."""
    _enable(landlord)
    provider = ScriptedProvider(
        [Turn(text="Let me check that."), Turn(text="Nothing is outstanding.")]
    )
    _turn(landlord, "anything owing?", provider)

    nudge = provider.requests[1]["messages"][-1]["text"]
    assert "NOW in this turn" in nudge
    assert "what it means for them" in nudge


def test_a_second_stall_is_not_shipped(landlord):
    """If it stalls twice, the landlord gets something deterministic rather
    than a second promise — they should never have to ask "and?" twice."""
    _enable(landlord)
    provider = ScriptedProvider(
        [Turn(text="I'll check the ledger now."), Turn(text="Let me look into it.")]
    )
    result = _turn(landlord, "is the $100 in?", provider)

    assert promises(result.reply, []) is False
    assert result.deterministic is True


def test_the_stall_is_recorded_for_review(landlord):
    """Every guard in this engine audits what it caught, so the failure rate
    is measurable rather than anecdotal."""
    _enable(landlord)
    provider = ScriptedProvider(
        [Turn(text="Checking the ledger now."), Turn(text="It's all paid.")]
    )
    _turn(landlord, "status?", provider)

    assert RamaAudit.objects.filter(
        landlord=landlord,
        kind=RamaAudit.Kind.ERROR,
        content__error="promised_without_delivering",
    ).exists()


def test_an_ordinary_answer_costs_no_extra_round(landlord):
    """The continuation must be on the stall path only — a normal turn is
    unchanged, and unchanged means one provider call."""
    _enable(landlord)
    provider = ScriptedProvider([Turn(text="You have three properties.")])
    result = _turn(landlord, "how many properties?", provider)

    assert result.reply == "You have three properties."
    assert len(provider.requests) == 1


def test_a_preview_is_never_treated_as_a_stall(landlord):
    """"I'll record the payment once you confirm" is the confirm contract
    working, not a stall. Catching it would break every write flow."""
    assert promises("I'll record the $100 payment. Confirm?", []) is False


# ------------------------------------------ a warning is not the model's to drop
def test_a_preview_warning_reaches_the_landlord(landlord, bc_lease):
    """For a SINGLE write the reply is the model's own prose — only batches get
    a deterministic renderer. So a warning the tool computed could simply be
    left out of the sentence the landlord actually reads. It is appended."""
    import datetime
    from decimal import Decimal

    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import EntryType
    from rentium.rama.providers import ToolCall

    charge, _ = ledger_services.post_charge(
        landlord=landlord,
        property=bc_lease.property,
        lease=bc_lease,
        tenant=None,
        entry_type=EntryType.DEPOSIT_CHARGE,
        amount=Decimal("425.00"),
        due_date=datetime.date.today(),
        description="Security deposit",
    )
    assert charge is not None

    _enable(landlord)
    provider = ScriptedProvider(
        [
            Turn(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="record_payment",
                        arguments={
                            "charge_query": "deposit",
                            # More than is owing — the tool computes an
                            # overpayment_warning the model must not swallow.
                            "amount": "900.00",
                            "payment_method": "etransfer",
                        },
                    )
                ],
            ),
            # The model relays a tidy summary and omits the warning entirely.
            Turn(text="Ready to record $900.00 against the deposit. Confirm?"),
        ]
    )
    result = _turn(landlord, "record $900 for the deposit", provider)

    assert "MORE than" in result.reply, result.reply
