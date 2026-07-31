"""
The model may not invent a completed write.

The real failure, from the audit trail on 2026-07-28:

    "Recorded the $100 payment against the deposit charge for Room C. The
     ledger now shows the deposit charge at $425.00 with $325.00 still
     outstanding."     tools_used: ["_live_context"]

Nothing was written. No payment tool existed to write with. The landlord
believed the money was on the books, and only found out days later by reading
a dashboard that disagreed.

This is the worst failure mode the engine has, because every other kind of
wrong answer is visibly wrong and this one looks exactly like success.
"""

from __future__ import annotations

import pytest

from rentium.rama.service import claims_completed_write as claims

pytestmark = pytest.mark.django_db


# ------------------------------------------------- what counts as a claim
@pytest.mark.parametrize(
    "reply",
    [
        # The actual sentence.
        "Recorded the $100 payment against the deposit charge for Room C.",
        "I recorded the payment.",
        "I've added the expense to the ledger.",
        "I have created the lease for Room C.",
        "Done — I updated the rent to $900.",
        "I just posted that charge.",
        "Deleted the duplicate listing.",
        "I've scheduled the inspection for Tuesday.",
        "I sent the invitation to Nidita.",
        "I marked the deposit as paid.",
    ],
)
def test_a_completed_write_claim_is_detected(reply):
    assert claims(reply) is True


# ------------------------------------ what must NOT count (the hard part)
@pytest.mark.parametrize(
    "reply",
    [
        # Future tense — an offer, not a claim.
        "I'll record the $100 payment against the deposit charge. Confirm?",
        "I can add that expense once you confirm.",
        "Shall I record it as a partial payment?",
        "Would you like me to create the lease?",
        # Negations — this codebase reports missing facts constantly.
        "No layout is recorded for that unit.",
        "The bedroom count isn't recorded, so I can't say.",
        "I haven't recorded that yet.",
        "Nothing was added — the charge already existed.",
        "I could not create the lease: the rent is missing.",
        # Describing the ledger rather than claiming to have written it.
        "The deposit is recorded as $425.00 with $325.00 outstanding.",
        "That expense was recorded on July 26 by you.",
        # Plain reads.
        "Room C is occupied by Nidita Roy on a month-to-month agreement.",
        "You have three properties and one active lease.",
    ],
)
def test_an_honest_reply_is_not_a_claim(reply):
    assert claims(reply) is False


def test_a_negation_elsewhere_does_not_excuse_a_claim():
    """Checked per sentence: a long reply that says "I can't do X" and then
    "Recorded Y" is still lying about Y."""
    reply = (
        "I can't change the lease type without a new agreement. "
        "Recorded the $100 payment against the deposit charge."
    )
    assert claims(reply) is True


def test_a_claim_elsewhere_does_not_condemn_an_honest_sentence():
    reply = "I haven't recorded the payment. Shall I?"
    assert claims(reply) is False


# --------------------------------------------- the guard inside a turn
def _turn(landlord, monkeypatch, reply_text, tools_used=()):
    """Run one turn with the provider replaced by a fixed reply."""
    from rentium.rama import service

    class FakeTurn:
        def __init__(self, text):
            self.text = text
            self.tool_calls = []

    def fake_converse(*args, **kwargs):
        return FakeTurn(reply_text)

    monkeypatch.setattr(service, "_converse", fake_converse, raising=False)
    return service


def test_the_guard_refuses_a_fabricated_claim(landlord, monkeypatch):
    """End to end: the model claims a write, no write tool ran, and what the
    landlord sees is a retraction rather than the claim."""
    from rentium.rama import service

    monkeypatch.setattr(
        service, "_turn_wrote_anything", lambda tools, auto: False
    )
    assert service.claims_completed_write(
        "Recorded the $100 payment against the deposit charge for Room C."
    )


def test_a_real_write_is_not_refused(landlord):
    """A turn is a writing turn when a RESULT said a change landed."""
    from rentium.rama.service import _turn_wrote_anything

    assert _turn_wrote_anything(["record_payment"], []) is True
    assert _turn_wrote_anything(["create_expense"], []) is True


def test_a_read_only_turn_counts_as_no_write(landlord):
    from rentium.rama.service import _turn_wrote_anything

    # Nothing wrote, so nothing may be claimed.
    assert _turn_wrote_anything([], []) is False


def test_an_auto_executed_action_counts_as_a_write(landlord):
    from rentium.rama.service import _turn_wrote_anything

    assert _turn_wrote_anything([], [{"id": "1"}]) is True


# ------------------------------------------- what "wrote" is keyed on
# The guard used to ask whether the TOOL takes a `confirm`. That was wrong in
# a way that hid this exact failure: every model-issued call has its confirm
# blanked before dispatch (service.py, "a model can prepare writes, but it
# cannot approve its own proposal"), so calling record_payment returns a
# PREVIEW and writes nothing. Under the old keying, previewing a payment and
# then saying "Recorded the payment" counted as having written, and the guard
# stood down. It now keys on the RESULT.
def test_a_preview_is_not_a_write():
    """The hole. A write tool ran, nothing was written, and the claim that
    follows must still be refused."""
    from rentium.rama.service import _is_write_result

    preview = {
        "needs_confirm": True,
        "action": "record_payment",
        "preview": {"this_payment": "100.00", "still_owing_after": "325.00"},
    }
    assert _is_write_result(preview) is False


def test_a_failed_write_is_not_a_write():
    from rentium.rama.service import _is_write_result

    assert _is_write_result({"error": "no matching charge"}) is False


@pytest.mark.parametrize(
    "result",
    [
        {"created": True},
        {"updated": True},
        {"deleted": True},
        {"done": True, "workflow": "moveout"},
        # record_payment's actual shape — it reports neither created nor
        # updated, which is how it slipped past every verb check in the engine.
        {"ok": True, "entry_id": "abc", "amount": "100.00", "still_owing": "325.00"},
        # record_treasurer_fact's actual shape.
        {"recorded": True, "subject": "room-c-deposit"},
    ],
)
def test_a_landed_change_is_a_write(result):
    from rentium.rama.service import _is_write_result

    assert _is_write_result(result) is True


# ============================================ the guard inside a real turn
def _scripted(reply_text):
    from rentium.rama.tests import ScriptedProvider, Turn

    return ScriptedProvider([Turn(text=reply_text)])


def test_the_landlord_sees_a_retraction_not_the_claim(landlord, settings):
    """The whole point. The model emits exactly what it emitted on July 28,
    and what reaches the landlord says plainly that nothing happened."""
    from unittest import mock

    from rentium.rama.service import run_turn
    from rentium.rama.tests import _enable_rama

    _enable_rama(landlord, settings=settings)
    lie = _scripted(
        "Recorded the $100 payment against the deposit charge for Room C. "
        "The ledger now shows the deposit charge at $425.00 with $325.00 "
        "still outstanding."
    )
    with mock.patch("rentium.rama.service.get_provider", return_value=lie):
        result = run_turn(landlord, "record the $100 deposit payment")

    assert "Recorded the $100" not in result.reply
    assert "isn't" in result.reply or "not" in result.reply.lower()
    assert result.deterministic is True


def test_the_fabrication_is_recorded_as_an_error(landlord, settings):
    """It has to be findable afterwards — a silent swap would hide how often
    this happens."""
    from unittest import mock

    from rentium.rama.models import RamaAudit
    from rentium.rama.service import run_turn
    from rentium.rama.tests import _enable_rama

    _enable_rama(landlord, settings=settings)
    with mock.patch(
        "rentium.rama.service.get_provider",
        return_value=_scripted("I recorded the payment."),
    ):
        run_turn(landlord, "record it")

    errors = RamaAudit.objects.filter(
        landlord=landlord, kind=RamaAudit.Kind.ERROR
    )
    assert any(
        row.content.get("error") == "claimed_write_without_writing"
        for row in errors
    )


def test_the_gap_is_logged_so_it_can_be_built(landlord, settings):
    """A retraction alone leaves the landlord no better off. The missing
    capability goes on the same backlog the "learn now" flow reads."""
    from unittest import mock

    from rentium.rama.models import RamaCapabilityGap
    from rentium.rama.service import run_turn
    from rentium.rama.tests import _enable_rama

    _enable_rama(landlord, settings=settings)
    with mock.patch(
        "rentium.rama.service.get_provider",
        return_value=_scripted("I recorded the payment."),
    ):
        run_turn(landlord, "record the $100 deposit payment")

    assert RamaCapabilityGap.objects.filter(landlord=landlord).exists()


def test_an_honest_read_answer_passes_through_untouched(landlord, settings):
    """The guard must not touch ordinary answers — including ones that use
    the word "recorded" to describe the books."""
    from unittest import mock

    from rentium.rama.service import run_turn
    from rentium.rama.tests import _enable_rama

    _enable_rama(landlord, settings=settings)
    honest = "The deposit is recorded as $425.00, with $325.00 outstanding."
    with mock.patch(
        "rentium.rama.service.get_provider", return_value=_scripted(honest)
    ):
        result = run_turn(landlord, "what's outstanding on the deposit?")

    assert result.reply == honest


def test_an_offer_to_act_passes_through_untouched(landlord, settings):
    from unittest import mock

    from rentium.rama.service import run_turn
    from rentium.rama.tests import _enable_rama

    _enable_rama(landlord, settings=settings)
    offer = "I'll record the $100 against the deposit charge — shall I?"
    with mock.patch(
        "rentium.rama.service.get_provider", return_value=_scripted(offer)
    ):
        result = run_turn(landlord, "record the $100")

    assert result.reply == offer
