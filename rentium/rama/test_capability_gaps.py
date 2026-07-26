"""The capability-gap backlog: dedupe, triage, and a way to see it.

RAMA already logged what it couldn't do. Two things stopped that being a
worklist: near-identical restatements each became their own row (the real
backlog accumulated five separate co-landlord gaps, each a rewording of the
last), and the list was only readable from inside a chat.
"""

import pytest
from rest_framework.test import APIClient

from rentium.rama import registry
from rentium.rama.models import RamaCapabilityGap

pytestmark = pytest.mark.django_db


def _log(landlord, request_text, **kw):
    return registry.execute(
        "log_capability_gap", {"request": request_text, **kw}, landlord=landlord
    )


# ------------------------------------------------------------------ dedupe
def test_the_same_ask_reworded_does_not_open_a_second_gap(landlord):
    """The five-rows-for-one-request problem."""
    _log(
        landlord,
        "Add a co-landlord (Sarbjeet Kaur) to the lease for Room C.",
    )
    _log(
        landlord,
        "Add a co-landlord (Sarbjeet Kaur, sarbjitkaur9@hotmail.com) to the "
        "lease for Room C.",
    )

    assert RamaCapabilityGap.objects.filter(landlord=landlord).count() == 1


def test_a_genuinely_different_ask_opens_its_own_gap(landlord):
    _log(landlord, "Add a co-landlord to the lease for Room C.")
    _log(landlord, "Export every tenant's payment history to a spreadsheet.")

    assert RamaCapabilityGap.objects.filter(landlord=landlord).count() == 2


def test_a_restatement_keeps_the_fuller_detail(landlord):
    """A second attempt often explains more than the first did."""
    _log(landlord, "Add a co-landlord to Room C's lease.", detail="Not supported.")
    _log(
        landlord,
        "Add a co-landlord to Room C's lease.",
        detail="The system only supports one landlord per lease; needs a "
        "team-access model and a scoping choke-point.",
    )

    gap = RamaCapabilityGap.objects.get(landlord=landlord)
    assert "team-access model" in gap.detail


def test_restating_can_raise_priority_but_never_lowers_it(landlord):
    _log(landlord, "Add a co-landlord to Room C's lease.", learn_now="yes")
    _log(landlord, "Add a co-landlord to Room C's lease.")

    gap = RamaCapabilityGap.objects.get(landlord=landlord)
    assert gap.prioritised is True


def test_a_decided_gap_raised_again_is_new_information(landlord):
    """Asking for something already dismissed is a signal, not a duplicate."""
    _log(landlord, "Add a co-landlord to Room C's lease.")
    RamaCapabilityGap.objects.update(status=RamaCapabilityGap.Status.DISMISSED)

    _log(landlord, "Add a co-landlord to Room C's lease.")
    assert RamaCapabilityGap.objects.filter(landlord=landlord).count() == 2


def test_gaps_never_leak_between_landlords(landlord):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    other = LandlordProfile.objects.create(user=UserFactory())
    _log(landlord, "Add a co-landlord to Room C's lease.")
    _log(other, "Add a co-landlord to Room C's lease.")

    assert RamaCapabilityGap.objects.filter(landlord=landlord).count() == 1
    assert RamaCapabilityGap.objects.filter(landlord=other).count() == 1


# ------------------------------------------------------------------ triage
def test_triage_moves_a_gap_and_previews_first(landlord):
    _log(landlord, "Export payment history to a spreadsheet.")
    gap = RamaCapabilityGap.objects.get(landlord=landlord)

    preview = registry.execute(
        "triage_capability_gap",
        {"gap_query": "payment history", "status": "BUILT"},
        landlord=landlord,
    )
    assert preview.get("needs_confirm")
    gap.refresh_from_db()
    assert gap.status == RamaCapabilityGap.Status.NEW

    registry.execute(
        "triage_capability_gap",
        {"gap_query": "payment history", "status": "BUILT", "confirm": "yes"},
        landlord=landlord,
    )
    gap.refresh_from_db()
    assert gap.status == RamaCapabilityGap.Status.BUILT


def test_triage_rejects_an_invented_status(landlord):
    _log(landlord, "Export payment history to a spreadsheet.")
    out = registry.execute(
        "triage_capability_gap",
        {"gap_query": "payment history", "status": "ALMOST", "confirm": "yes"},
        landlord=landlord,
    )
    assert "status must be one of" in out.get("error", "")


def test_triage_asks_which_one_when_several_match(landlord):
    _log(landlord, "Export payment history to a spreadsheet.")
    _log(landlord, "Export payment history as a PDF report for the accountant.")

    out = registry.execute(
        "triage_capability_gap",
        {"gap_query": "payment history", "status": "BUILT", "confirm": "yes"},
        landlord=landlord,
    )
    assert out.get("candidates")


# --------------------------------------------------------------------- API
def test_the_backlog_is_readable_outside_a_chat(landlord):
    _log(landlord, "Export payment history to a spreadsheet.", learn_now="yes")

    api = APIClient()
    api.force_authenticate(user=landlord.user)
    body = api.get("/api/rama/capability-gaps/").json()

    assert body["counts"]["NEW"] == 1
    assert body["gaps"][0]["prioritised"] is True


def test_the_backlog_can_be_triaged_over_the_api(landlord):
    _log(landlord, "Export payment history to a spreadsheet.")
    gap = RamaCapabilityGap.objects.get(landlord=landlord)

    api = APIClient()
    api.force_authenticate(user=landlord.user)
    resp = api.patch(
        "/api/rama/capability-gaps/",
        {"id": str(gap.pk), "status": "REVIEWED", "prioritised": True},
        format="json",
    )
    assert resp.status_code == 200

    gap.refresh_from_db()
    assert gap.status == RamaCapabilityGap.Status.REVIEWED
    assert gap.prioritised is True


def test_another_landlords_gap_cannot_be_triaged(landlord):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    other = LandlordProfile.objects.create(user=UserFactory())
    _log(other, "Export payment history to a spreadsheet.")
    gap = RamaCapabilityGap.objects.get(landlord=other)

    api = APIClient()
    api.force_authenticate(user=landlord.user)
    resp = api.patch(
        "/api/rama/capability-gaps/",
        {"id": str(gap.pk), "status": "DISMISSED"},
        format="json",
    )
    assert resp.status_code == 404

    gap.refresh_from_db()
    assert gap.status == RamaCapabilityGap.Status.NEW
