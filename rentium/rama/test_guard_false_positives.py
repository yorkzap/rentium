"""The reply guards must be pinned in BOTH directions.

`claims_completed_write` fires only when the turn wrote nothing — that is,
exclusively on read-only answers. It shares its whole vocabulary with the read
tools it is judging: charge_schedule's own status words are scheduled / unpaid /
paid / partially_paid, so "Scheduled viewings for August:" was being condemned
as a fabricated write and the landlord's answer thrown away and replaced with
"I said that as though it were done — it isn't." Seven of nine realistic read
answers were destroyed this way.

Every existing test here asserted the true positives. None asserted that a
correct answer survives, so nothing failed while the guard ate them. Both
directions now live in one file, side by side, because tightening either one
without looking at the other is how this happened.
"""

from __future__ import annotations

import pytest

from rentium.rama.service import asks_for_what_the_books_hold
from rentium.rama.service import claims_completed_write

# ---------------------------------------------------------------- fabrications
# A write that did not happen, stated as though it had. Every one of these must
# stay caught; "Recorded as etransfer." in particular reached a real landlord in
# August 2026 and is the reason the subject-less branch exists at all.
FABRICATIONS = [
    "Recorded as etransfer.",
    "Recorded the $100 payment.",
    "I recorded the payment of $425.",
    "I've updated the rent to 900.",
    "Created a lease for Room C.",
    "Sent the invite to sarah@example.com.",
    "Posted the August rent charge.",
    "I just voided that charge.",
    "Marked the charge paid.",
    "Added the expense to the ledger.",
    "I have scheduled the viewing for Tuesday.",
    "Renamed the document to Lease-RoomC.pdf.",
    "Deleted that listing.",
    "I created the work order.",
    "Terminated the lease.",
    # A fabrication can carry more figures than a table row does. Suppressing
    # rows purely by counting numbers dropped this whole line as "a table" and
    # let the lie through — a row is numbers with few words, not the reverse.
    "Recorded the $100 payment against the deposit charge for Room C. The "
    "ledger now shows the deposit charge at $425.00 with $325.00 still "
    "outstanding.",
]

# ------------------------------------------------------------- honest reports
# Read-only answers, which is the ONLY kind of turn this guard runs on.
REPORTS = [
    "Scheduled viewings for August:\n• Room C — Aug 12, 2pm",
    "Sent: 3 invites are still unopened.",
    "Marked paid: 2 of 5 August charges.",
    "Updated leases in the last 30 days: none.",
    "Posted charges for August total 2,300.00.",
    "Cancelled viewings this month: 1.",
    "Invited tenants on RMT652523: Aishwarya, Naveen.",
    "Here are the August rents. Received: 1900.00 across 3 charges. "
    "Due: 869.78 across 2.",
    "Collected this month (Aug 2026): 1900.00.",
    "Scheduled 2026-09-01 850.00",
    "Posted 2026-08-01 — $850 rent (unpaid)",
    "Recorded: 2026-08-01, $850, unpaid.",
    "Removed items: none",
    "August charges:\n| Due | Amount | State |\n| 2026-08-01 | 850.00 | PAID |",
    "Outstanding charges:\n- Scheduled 2026-09-01 850.00\n"
    "- Posted 2026-08-01 850.00",
    # Honest denials and offers were already safe; keep them that way.
    "I haven't recorded that yet.",
    "Nothing was posted for August.",
    "Shall I record it?",
    "No layout recorded for Room C.",
    "The deposit is recorded as $425.",
]


@pytest.mark.parametrize("text", FABRICATIONS)
def test_a_fabricated_write_is_still_caught(text):
    assert claims_completed_write(text) is True


@pytest.mark.parametrize("text", REPORTS)
def test_an_honest_read_answer_survives(text):
    assert claims_completed_write(text) is False


# --------------------------------------------- asking for what the books hold

LOOKUP_DEMANDS = [
    (
        "I need the exact amounts from the lease to record the payments. What "
        "are the cleaning deposit and security deposit amounts for Siya's lease?",
        "siya's cleaning and security deposits were received",
    ),
    (
        "Can you tell me the outstanding balance on that lease?",
        "sort out room c",
    ),
]

LEGITIMATE = [
    # The sentence that cost a landlord their whole answer. It is an OFFER of
    # more work at the end of a complete reply, not a demand for data.
    (
        "Want me to pull the payment ledger lines so you can see who paid and "
        "which charges remain unpaid?",
        "how many rents did we receive for aug or are due?",
    ),
    # Restating the question as a heading is normal answer shape.
    (
        "How much rent is still due?",
        "how much rent is still due",
    ),
    # Facts that live outside the database must still be askable.
    ("Was that an e-transfer or cash?", "siya paid the deposit"),
    ("Which day did it land?", "the rent came in"),
    ("Which room did you mean?", "add a tenant"),
    ("Shall I proceed?", "raise the rent"),
]


@pytest.mark.parametrize(("reply", "asked"), LOOKUP_DEMANDS)
def test_a_lookup_demand_is_still_caught(reply, asked):
    assert asks_for_what_the_books_hold(reply, landlord_message=asked) is True


@pytest.mark.parametrize(("reply", "asked"), LEGITIMATE)
def test_a_legitimate_question_or_offer_survives(reply, asked):
    assert asks_for_what_the_books_hold(reply, landlord_message=asked) is False


def test_the_incident_reply_survives_intact():
    """The full August 2026 reply, end to end.

    The model answered correctly, offered a useful follow-up, and the offer
    tripped the guard — which routed into a retry that crashed, so the answer
    was never delivered. Both halves are fixed; this pins the whole text.
    """
    reply = (
        "Short answer from the dashboard (as of 2026-08-10):\n\n"
        "• Collected this month (Aug 2026): 1900.00.\n"
        "• Deposits collected this month: 400.00.\n"
        "• Outstanding (charges due on or before today): 869.78 across 2 charges.\n"
        "• Owed total (includes deposits/liabilities): 2194.78 across 5 items.\n"
        "• Next scheduled rent charge: 800.00 due 2026-09-01 (Room D).\n\n"
        "Want me to pull the payment ledger lines so you can see who paid and "
        "which charges remain unpaid?"
    )
    asked = "how many rents did we receive for aug or are due?"

    assert claims_completed_write(reply) is False
    assert asks_for_what_the_books_hold(reply, landlord_message=asked) is False
