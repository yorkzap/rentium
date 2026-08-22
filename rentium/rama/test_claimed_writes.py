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


# --------------------------------------- the verb list, kept in step
# A write flag missing from WRITE_RESULT_MARKERS makes a real change invisible
# to the guard, which then tells the landlord it didn't happen. `return_deposits`
# reports {"returned": True} and was invisible exactly this way. Rather than
# trusting anyone to remember, this reads the flags the tools actually set.
def test_every_write_flag_a_tool_sets_is_recognised():
    import re
    from pathlib import Path

    from rentium.rama.service import WRITE_RESULT_MARKERS

    # Keys that are data, a question, or a refusal on a result — never a
    # "this landed" flag. Anything not here and not in WRITE_RESULT_MARKERS is
    # a decision nobody has made yet, which is what this test forces.
    NOT_WRITE_FLAGS = {
        # Explicit non-writes.
        "already",
        "already_done",
        "unchanged",
        "refused",
        "reused",  # "already exists — reusing it, nothing changed"
        "exists",
        "needs_confirm",
        "needs_answer",
        "needs_input",
        "needs_scope",
        "required",
        # Descriptive data about the thing acted on.
        "atomic",
        "attached",
        "collection",
        "create",
        "damage_claim",
        "duplicate",
        "is_duplicate",
        "editable",
        "excluded",
        # Whether a filed document has a ledger consequence still to come.
        # The opposite of a write: it is how the preview says the spend is NOT
        # recorded yet.
        "expense_like",
        "hard",
        "has_file",
        "idempotent",
        "included",
        "landlord_signed",
        "linked_existing",
        "linked_existing_account",
        "ocr_complete",
        "raw_preserved",
        "returned_separately",
        "reuse_property",
        "supported",
        "user_scope_locked",
        # Prompt/UI hints the tool ships to the model or the frontend.
        "convert_to_ocr_document",
        "never_say_no_pdf_if_lease_exists",
        "pdf_always_available",
        "pdf_download_available",
        "reocr_requested",
        "ui_rules",
    }
    root = Path(__file__).resolve().parent
    sources = [
        *root.glob("domain_*.py"),
        root / "finance.py",
        root / "document_services.py",
        root / "landlord_capabilities.py",
        root / "house_layout.py",
        root / "unit_structure.py",
    ]
    # `"flag": True` inside a returned dict literal.
    flag = re.compile(r'"([a-z_]+)":\s*True')
    seen = set()
    for path in sources:
        if not path.exists():
            continue
        seen |= set(flag.findall(path.read_text()))

    unknown = sorted(seen - set(WRITE_RESULT_MARKERS) - NOT_WRITE_FLAGS)
    assert not unknown, (
        f"These result flags are set by tools but are not in "
        f"WRITE_RESULT_MARKERS (and are not listed as non-flags): {unknown}. "
        f"A write the guard can't see gets reported to the landlord as not "
        f"having happened."
    )


def test_returning_deposits_is_recognised_as_a_write():
    """The shape return_deposits actually reports."""
    from rentium.rama.service import _is_write_result

    assert _is_write_result({"returned": True, "lease_number": "RMT1-A"}) is True


def test_a_skipped_duplicate_is_not_a_write():
    from rentium.rama.service import _is_write_result

    assert (
        _is_write_result({"already_done": True, "message": "Already marked paid."})
        is False
    )


def test_an_unanswered_question_is_not_a_write():
    from rentium.rama.service import _is_write_result

    assert (
        _is_write_result(
            {"question_for_user": "Which deposit?", "needs": "deposit"}
        )
        is False
    )


# =========================== a follow-up about a write that DID happen
# The other direction of the same failure. A landlord confirmed a $350 cleaning
# deposit on lease RMT652523-C281, it was written, and one turn later they
# asked "u sure its added?". The model said yes — truthfully — nothing was
# written on THAT turn, and the guard replaced the true answer with "Nothing
# was written, so please don't rely on that." The deposit was on the lease the
# whole time, and the landlord was sent to re-do work that was already done.
#
# "Nothing was written" is a claim about their data too. It only gets made when
# the record supports it.
def _wrote_earlier(landlord, conversation_id, tool="update_lease", message="Updated lease RMT1-A: cleaning_deposit."):
    from rentium.rama.models import RamaAudit

    RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
        content={
            "tool": tool,
            "arguments": {"confirm": "yes"},
            "result": {"updated": True, "message": message},
        },
    )


def test_a_follow_up_about_a_real_write_is_not_retracted(landlord, settings):
    import uuid
    from unittest import mock

    from rentium.rama.service import run_turn
    from rentium.rama.tests import _enable_rama

    _enable_rama(landlord, settings=settings)
    conversation_id = uuid.uuid4()
    _wrote_earlier(landlord, conversation_id)

    with mock.patch(
        "rentium.rama.service.get_provider",
        return_value=_scripted("Yes — I added the $350 cleaning deposit."),
    ):
        result = run_turn(landlord, "u sure its added?", conversation_id)

    assert "Nothing was written" not in result.reply
    # It answers from the record rather than from the model's own memory.
    assert "cleaning_deposit" in result.reply
    assert result.deterministic is True


def test_the_record_answer_names_only_what_actually_landed(landlord, settings):
    """A real write earlier must not launder a fresh fabrication: the reply is
    built from the audit trail, so an invented second change simply isn't in
    it."""
    import uuid
    from unittest import mock

    from rentium.rama.service import run_turn
    from rentium.rama.tests import _enable_rama

    _enable_rama(landlord, settings=settings)
    conversation_id = uuid.uuid4()
    _wrote_earlier(landlord, conversation_id)

    with mock.patch(
        "rentium.rama.service.get_provider",
        return_value=_scripted(
            "I added the cleaning deposit and also recorded the $900 rent "
            "payment against August."
        ),
    ):
        result = run_turn(landlord, "did both go through?", conversation_id)

    assert "$900" not in result.reply
    assert "rent payment" not in result.reply.lower()
    assert "cleaning_deposit" in result.reply


def test_a_fabrication_in_a_conversation_that_never_wrote_still_retracts(
    landlord, settings
):
    """The original protection, unchanged."""
    import uuid
    from unittest import mock

    from rentium.rama.service import run_turn
    from rentium.rama.tests import _enable_rama

    _enable_rama(landlord, settings=settings)
    with mock.patch(
        "rentium.rama.service.get_provider",
        return_value=_scripted("I recorded the payment."),
    ):
        result = run_turn(landlord, "record it", uuid.uuid4())

    assert "Nothing was written" in result.reply


# ---------------------------------------------------------------------------
# August 2026: the $1,175 e-transfer that was never recorded
#
# A landlord said a tenant had paid $1,175. RAMA replied "Recorded as
# etransfer." and, on the next turn, produced a full preview — "Outstanding
# charges: $869.78 (2 items) … leave $305.22 as a credit … Confirm?" — having
# called no tool on either turn. Every figure was invented; the real ledger held
# $325 outstanding on the deposit and $850 of August rent, exactly what the
# landlord had said. Two guards missed it, for two different reasons.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        # The one that got through: the "recorded AS" exclusion was written for
        # the DESCRIPTIVE form and swallowed the active report as well.
        "Recorded as etransfer.",
        "Recorded as etransfer. I'll apply it to the outstanding ledger items "
        "for Room C and confirm the allocation with you before posting.",
        "Recorded as an e-transfer against the deposit.",
        "Logged as cash.",
    ],
)
def test_reporting_an_action_in_the_passive_voice_is_still_a_claim(reply):
    assert claims(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        # ...while the descriptive form it was protecting still passes. The
        # difference is the linking verb, not the word "as".
        "The deposit is recorded as $425.00 with $325.00 outstanding.",
        "The payment was recorded as an e-transfer on August 3.",
        "That $100 shows as recorded against the security deposit.",
    ],
)
def test_describing_the_books_is_still_not_a_claim(reply):
    assert claims(reply) is False


@pytest.mark.parametrize(
    ("solicits", "reply"),
    [
        (True, "Preview:\n- Outstanding charges: $869.78 (2 items)\n\nConfirm?"),
        (True, "I'll apply the $1,175 now. Please confirm."),
        (True, "Reply yes to record it, or no to cancel."),
        (True, "Shall I proceed?"),
        # Questions that need an answer, not an approval — a plan cannot exist
        # for these yet and they must not be intercepted.
        (False, "Which charge was this payment for?"),
        (False, "How did the $1,175 arrive — e-transfer, cash, or cheque?"),
        (False, "Nidita Roy gave a $100.00 security deposit."),
        (False, "Do you want the September rent prorated?"),
    ],
)
def test_only_approval_shapes_count_as_soliciting_confirmation(solicits, reply):
    from rentium.rama.service import _solicits_confirmation

    assert _solicits_confirmation(reply) is solicits


def test_a_preview_with_no_plan_behind_it_is_retracted(landlord, settings):
    """The dangerous one: it wears the exact shape of the real confirm flow.

    A genuine preview persists a plan, so "yes" runs a verified batch. This
    reply persisted nothing, so "yes" would have arrived at an empty plan while
    the landlord believed $1,175 had been allocated.
    """
    from unittest import mock

    from rentium.rama.service import run_turn
    from rentium.rama.tests import _enable_rama

    _enable_rama(landlord, settings=settings)
    fabricated = (
        "I'll apply the $1,175 etransfer to Room C's ledger now. Preview:\n\n"
        "- Outstanding charges: $869.78 (2 items)\n"
        "- Deposits held: $100.00\n"
        "- Total applied: $1,175.00\n\n"
        "This will clear the outstanding balance and leave $305.22 as a credit "
        "toward future rent or charges.\n\nConfirm?"
    )
    with mock.patch(
        "rentium.rama.service.get_provider", return_value=_scripted(fabricated)
    ):
        result = run_turn(landlord, "yes")

    assert result.deterministic is True
    assert result.pending_plan is None
    assert "869.78" not in result.reply
    assert "305.22" not in result.reply
    assert "no pending action" in result.reply.casefold()


# --------------------------------------------------------- headings vs claims

#: The verbatim reply that was retracted. The landlord asked which rooms were
#: recorded versus guessed, got a correct read-only answer, and was told "I
#: said that as though it were done — it isn't".
THE_RETRACTED_ANSWER = (
    "Quick summary — I used the unit/listing layout fields in the system. "
    "“Recorded” means the listing or its parent unit has explicit "
    "layout fields.\n"
    "\n"
    "Recorded on the listing itself\n"
    "• Bonus room J — kitchen, balcony, bathroom, bedroom recorded "
    "(recorded_internal_area_count 4).\n"
    "• Room K — 1 bedroom recorded (recorded_internal_area_count 1).\n"
)


def test_a_section_heading_is_not_a_write_claim():
    """The landlord's own question contained the verb, so the answer had to."""
    assert claims(THE_RETRACTED_ANSWER) is False


@pytest.mark.parametrize(
    "reply",
    [
        "Recorded on the listing itself\n• Room K — 1 bedroom",
        "Recorded from the unit layout\n- Main Floor: 3 bedrooms",
        "Updated by the last inspection\n1. Room C — clean",
        "Sent for August\n* 3 reminders",
    ],
)
def test_headings_over_a_list_pass_through(reply):
    assert claims(reply) is False


@pytest.mark.parametrize(
    "reply",
    [
        # Still a claim: an object behind the verb, whatever follows it.
        "Recorded the $100 payment against the deposit charge for Room C\n"
        "• e-transfer\n• August 3",
        "Recorded the payment\n• $100 e-transfer",
        "Updated your lease\n• rent now $2,000",
        # Still a claim: it is a sentence, not a heading.
        "Recorded as etransfer.\n• Room C",
        "Logged as cash.\n• $400",
        # Still a claim: nothing follows, so it heads nothing.
        "Recorded on the ledger",
        "Recorded payment",
    ],
)
def test_a_fabrication_dressed_as_a_heading_is_still_caught(reply):
    assert claims(reply) is True


def test_the_retraction_audit_records_the_question(landlord, settings):
    """A misfire must be diagnosable from the audit trail alone.

    This guard has now misfired in three directions; each time the only way to
    see why was to reproduce the whole turn against live data.
    """
    import inspect

    from rentium.rama import service

    source = inspect.getsource(service)
    assert '"asked": (message or "")[:300]' in source


# ------------------------------------- an unbacked "yes" must not eat the answer

def test_a_grounded_answer_survives_the_missing_plan():
    """Retract the ask, keep what was found.

    Asked which rooms were recorded versus guessed, RAMA read property_area
    twice, answered correctly, offered to fill the gaps — and the landlord got
    "I couldn't prepare an executable plan. Please resend the changes" and none
    of the answer. Same defect as a budget stop discarding ten good reads.
    """
    from rentium.rama.service import _read_something_real, _strip_confirmation_ask

    assert _read_something_real(["_live_context", "read"]) is True
    assert _read_something_real(["_live_context"]) is False
    assert _read_something_real([]) is False

    reply = (
        "Recorded layout\n"
        "• Main Floor — 3 bedrooms, 2 bathrooms\n"
        "• Garden Suite — 2 bedrooms\n"
        "Shall I proceed?"
    )
    kept = _strip_confirmation_ask(reply)
    assert "Main Floor — 3 bedrooms, 2 bathrooms" in kept
    assert "Shall I proceed?" not in kept


def test_stripping_never_returns_nothing():
    """A reply that is ONLY an ask still has to say something."""
    from rentium.rama.service import _strip_confirmation_ask

    assert _strip_confirmation_ask("Shall I proceed?").strip()
    assert _strip_confirmation_ask("").strip() == ""
