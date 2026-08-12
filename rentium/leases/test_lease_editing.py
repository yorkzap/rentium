"""Editing a lease that isn't executed yet.

A lease is editable until `Lease.is_locked()` — ACTIVE and beyond. Before then
it can already carry signatures: the landlord's, and any tenant who signed
early. Editing is allowed in that state, deliberately (the landlord owns the
document until it is executed), but a material change writes an immutable
TERMS_AMENDED event against everyone who had already signed. Nothing is sent to
them — the landlord decides who to tell; the record exists so they can see, and
later prove, who agreed to what.

These tests lock in the two halves of that: the edit goes through, and the
evidence gets written. Plus the ACTIVE lock, which is what stops an executed
lease and its posted ledger charges drifting apart.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from rentium.leases.models import Lease, LeaseInviteEvent, LeaseTenant, RentAdjustment
from rentium.leases.services import update_lease_record

pytestmark = pytest.mark.django_db


@pytest.fixture
def pending_lease(landlord, bc_property):
    lease = Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date(2026, 9, 1),
        end_date=date(2027, 8, 31),
        total_rent=Decimal("1800.00"),
        security_deposit=Decimal("200.00"),
        cleaning_deposit=Decimal("200.00"),
    )
    LeaseTenant.objects.create(
        lease=lease,
        invited_email="early@example.com",
        invited_name="Early Signer",
        rent_amount=Decimal("900.00"),
        is_primary_tenant=True,
        has_signed=True,
        signed_date=date(2026, 7, 12),
    )
    LeaseTenant.objects.create(
        lease=lease,
        invited_email="late@example.com",
        invited_name="Not Yet",
        rent_amount=Decimal("900.00"),
    )
    return lease


def _client(landlord):
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    return client


def _amendments(lease):
    return LeaseInviteEvent.objects.filter(
        lease_tenant__lease=lease,
        kind=LeaseInviteEvent.Kind.TERMS_AMENDED,
    )


def test_a_pending_lease_is_editable_even_with_a_signature_on_it(
    landlord, pending_lease
):
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"cleaning_deposit": "250.00", "total_rent": "1900.00"},
        format="json",
    )

    assert response.status_code == 200, response.data
    pending_lease.refresh_from_db()
    assert pending_lease.cleaning_deposit == Decimal("250.00")
    assert pending_lease.total_rent == Decimal("1900.00")


def test_move_in_change_keeps_explicit_lease_period_target(pending_lease, landlord):
    pending_lease.start_date = date(2026, 8, 1)
    pending_lease.move_in_date = date(2026, 8, 1)
    pending_lease.save(update_fields=["start_date", "move_in_date", "updated_at"])
    slot = pending_lease.lease_tenants.get(is_primary_tenant=True)
    adjustment = RentAdjustment.objects.create(
        lease_tenant=slot,
        adjustment_type=RentAdjustment.AdjustmentType.DISCOUNT,
        calculation_method=RentAdjustment.CalculationMethod.FLAT_AMOUNT,
        amount=Decimal("500.00"),
        target_amount=Decimal("400.00"),
        reason="First-month household rent target",
        effective_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        is_recurring=False,
        created_by=landlord,
    )

    result = update_lease_record(
        landlord=landlord,
        lease=pending_lease,
        values={"move_in_date": date(2026, 9, 1)},
    )

    adjustment.refresh_from_db()
    assert result["rebased_adjustments"] == []
    assert adjustment.effective_date == date(2026, 8, 1)
    assert adjustment.end_date == date(2026, 8, 31)
    assert adjustment.target_amount == Decimal("400.00")


def test_amending_terms_records_it_against_whoever_already_signed(
    landlord, pending_lease
):
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"cleaning_deposit": "250.00"},
        format="json",
    )

    assert response.data["amended_signers"] == ["Early Signer"]
    events = list(_amendments(pending_lease))
    assert len(events) == 1, "one event, for the one tenant who had signed"
    assert events[0].lease_tenant.invited_email == "early@example.com"
    assert events[0].metadata["fields"] == ["cleaning_deposit"]
    assert events[0].metadata["before"] == {"cleaning_deposit": "200.00"}
    assert events[0].metadata["after"] == {"cleaning_deposit": "250.00"}


def test_the_tenant_is_not_notified_of_an_amendment(landlord, pending_lease, mailoutbox):
    """Landlord control is the product decision: the record is written, the
    email is not sent."""
    _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"total_rent": "1900.00"},
        format="json",
    )
    assert mailoutbox == []


def test_a_cosmetic_edit_is_not_an_amendment(landlord, pending_lease):
    """Fixing the notice address isn't changing the deal, so it doesn't
    accuse anyone of having signed something different."""
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"landlord_service_address": "12 New Office Rd"},
        format="json",
    )

    assert response.status_code == 200, response.data
    assert response.data["amended_signers"] == []
    assert not _amendments(pending_lease).exists()


def test_an_active_lease_cannot_be_edited(landlord, bc_lease):
    """bc_lease is ACTIVE: its document is frozen and its deposit and rent
    charges are already posted. LeaseNotLocked refuses before anything else."""
    response = _client(landlord).patch(
        f"/api/leases/{bc_lease.pk}/",
        {"total_rent": "9999.00"},
        format="json",
    )

    assert response.status_code == 403
    bc_lease.refresh_from_db()
    assert bc_lease.total_rent == Decimal("850.00")


def test_status_cannot_be_patched_straight_to_active(landlord, pending_lease):
    """Activation runs check_and_activate(), which freezes the signed document,
    posts the deposit and rent charges and opens occupancy. PATCHing the field
    skipped all three."""
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"status": "ACTIVE"},
        format="json",
    )

    assert response.status_code == 200
    pending_lease.refresh_from_db()
    assert pending_lease.status == Lease.LeaseStatus.PENDING_SIGNATURES


def test_agreement_terms_fields_are_now_reachable_from_the_api(
    landlord, pending_lease
):
    """rent_due_day and the parking block print into the agreement but had no
    route in from anywhere but Django admin."""
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {
            "rent_due_day": 15,
            "parking_included": True,
            "parking_description": "One stall, #14",
            # pets_terms is refused unless pets are actually allowed — the
            # model won't let the agreement contradict itself.
            "pets_allowed": True,
            "pets_terms": "One cat under 15lb",
        },
        format="json",
    )

    assert response.status_code == 200, response.data
    pending_lease.refresh_from_db()
    assert pending_lease.rent_due_day == 15
    assert pending_lease.parking_included is True
    assert pending_lease.parking_description == "One stall, #14"
    assert pending_lease.pets_terms == "One cat under 15lb"


def test_changing_a_signed_tenants_rent_share_is_allowed_and_recorded(
    landlord, pending_lease
):
    """It used to be refused outright, leaving Django admin as the only route.
    It is an amendment now, not a wall."""
    slot = pending_lease.lease_tenants.get(invited_email="early@example.com")
    response = _client(landlord).patch(
        f"/api/leases/tenants/{slot.pk}/",
        {"rent_amount": "1000.00"},
        format="json",
    )

    assert response.status_code == 200, response.data
    slot.refresh_from_db()
    assert slot.rent_amount == Decimal("1000.00")
    event = _amendments(pending_lease).get()
    assert event.metadata["after"] == {"rent_amount": "1000.00"}


def test_rama_and_the_dashboard_share_one_edit_path(landlord, pending_lease):
    """The amendment record can't be dodged by editing from the other door."""
    from rentium.rama import registry

    result = registry.execute(
        "update_lease",
        {
            "lease_number": pending_lease.lease_number,
            "cleaning_deposit": "300.00",
            "confirm": "yes",
        },
        landlord=landlord,
    )

    assert result["updated"] is True
    assert result["amended_signers"] == ["Early Signer"]
    assert "not been notified" in result["message"]
    assert _amendments(pending_lease).count() == 1


# ============ "any field, before all parties have signed"
def test_co_hosts_are_editable_on_an_unsigned_lease(landlord, pending_lease):
    """They were create-only, so a typo in a co-host's email meant recreating
    the lease. They carry no signature — a co-landlord who must sign is a
    LeaseLandlordSignatory, invited through its own endpoint."""
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"co_hosts": [{"name": "Ana Ruiz", "email": "ana@example.com"}]},
        format="json",
    )

    assert response.status_code == 200, response.data
    pending_lease.refresh_from_db()
    assert pending_lease.co_hosts == [
        {"name": "Ana Ruiz", "email": "ana@example.com", "phone": ""}
    ]


def test_a_co_host_without_a_name_is_refused(landlord, pending_lease):
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"co_hosts": [{"email": "nameless@example.com"}]},
        format="json",
    )
    assert response.status_code == 400


def test_a_signed_tenants_name_can_still_be_corrected(landlord, pending_lease):
    """It used to freeze at signature. That name prints in the parties and
    signature blocks, so a typo was unfixable without deleting the invite."""
    slot = pending_lease.lease_tenants.get(invited_email="early@example.com")
    response = _client(landlord).patch(
        f"/api/leases/tenants/{slot.pk}/",
        {"invited_name": "Early Signer-Jones"},
        format="json",
    )

    assert response.status_code == 200, response.data
    slot.refresh_from_db()
    assert slot.invited_name == "Early Signer-Jones"


def test_a_linked_tenants_name_still_cannot_be_overwritten(landlord, pending_lease):
    """Once they have an account, their own name is authoritative — writing
    over it would put a name on the agreement they never chose."""
    from rentium.users.models import TenantProfile
    from rentium.users.tests.factories import UserFactory

    slot = pending_lease.lease_tenants.get(invited_email="late@example.com")
    slot.tenant = TenantProfile.objects.create(user=UserFactory())
    slot.save(update_fields=["tenant"])

    response = _client(landlord).patch(
        f"/api/leases/tenants/{slot.pk}/",
        {"invited_name": "Something Else"},
        format="json",
    )
    assert response.status_code == 400
    assert "linked their account" in str(response.data)


def test_the_editor_is_told_which_services_exist(landlord, pending_lease):
    """The list drives which fair-use terms print, so it ships from the server
    rather than being hardcoded in the frontend."""
    response = _client(landlord).get(f"/api/leases/{pending_lease.pk}/")

    choices = response.data["service_choices"]
    values = [c["value"] for c in choices]
    assert "HEAT" in values and "ELECTRICITY" in values
    assert all(set(c) == {"value", "label"} for c in choices)


def test_services_included_are_editable_and_change_the_agreement(
    landlord, pending_lease
):
    from rentium.leases.documents import render_lease

    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"services_and_facilities": ["HEAT", "WATER"], "occupants": ["A child"]},
        format="json",
    )

    assert response.status_code == 200, response.data
    pending_lease.refresh_from_db()
    assert pending_lease.services_and_facilities == ["HEAT", "WATER"]
    assert pending_lease.occupants == ["A child"]

    text = "\n".join(
        c for s in render_lease(pending_lease).sections for c in s.clauses
    )
    assert "Heat: the tenant should not run the heating" in text
    assert "Water: taps, showers and hoses" in text


# ------------------------------------------- amending a lease that is LIVE
#
# "in the leases edit ui, there should be option to edit the special terms too"
#
# Freezing an executed lease completely was right for the deal and wrong for
# everything around it. A landlord who agrees a new house rule mid-tenancy, or
# whose service address changes, had no route but Django admin — while BC
# expects the current terms to be the ones on record.
#
# So the split is by what has machinery behind it: wording may be amended, the
# deal may not, and a lease past ACTIVE is not editable at all.
def test_an_active_lease_accepts_an_amendment_to_its_wording(landlord, bc_lease):
    response = _client(landlord).patch(
        f"/api/leases/{bc_lease.pk}/",
        {"special_terms": "Tenant maintains the garden beds from June."},
        format="json",
    )

    assert response.status_code == 200, response.data
    bc_lease.refresh_from_db()
    assert bc_lease.special_terms == "Tenant maintains the garden beds from June."


def test_an_active_lease_still_refuses_the_deal_itself(landlord, bc_lease):
    """Rent has charges posted against it and the dates drive notice periods
    and the deposit-return clock. Those move through the ledger, not a text
    box — which is the whole reason the lock existed."""
    for field, value in (
        ("total_rent", "9999.00"),
        ("security_deposit", "1.00"),
        ("end_date", "2030-01-01"),
        ("start_date", "2020-01-01"),
    ):
        response = _client(landlord).patch(
            f"/api/leases/{bc_lease.pk}/", {field: value}, format="json",
        )
        assert response.status_code == 403, (field, response.data)

    bc_lease.refresh_from_db()
    assert bc_lease.total_rent == Decimal("850.00")


def test_one_frozen_field_refuses_the_whole_patch(landlord, bc_lease):
    """A mixed PATCH must not half-apply — the wording landing while the rent
    is rejected would leave the landlord believing both went through."""
    before = bc_lease.special_terms
    response = _client(landlord).patch(
        f"/api/leases/{bc_lease.pk}/",
        {"special_terms": "Amended.", "total_rent": "9999.00"},
        format="json",
    )

    assert response.status_code == 403
    bc_lease.refresh_from_db()
    assert bc_lease.special_terms == before
    assert bc_lease.total_rent == Decimal("850.00")


def test_the_refusal_says_what_to_do_instead(landlord, bc_lease):
    response = _client(landlord).patch(
        f"/api/leases/{bc_lease.pk}/", {"total_rent": "9999.00"}, format="json",
    )
    detail = str(response.data.get("detail") or response.data)
    assert "rent adjustment" in detail.casefold()


def test_an_ended_lease_is_frozen_completely(landlord, bc_lease):
    """History is not editable. A terminated tenancy's record is what a dispute
    is argued from, so even its wording stops moving."""
    from rentium.leases.models import Lease

    bc_lease.status = Lease.LeaseStatus.TERMINATED
    bc_lease.save(update_fields=["status"])

    response = _client(landlord).patch(
        f"/api/leases/{bc_lease.pk}/",
        {"special_terms": "Rewriting history."},
        format="json",
    )
    assert response.status_code == 403


def test_a_full_replace_is_refused_on_a_live_lease(landlord, bc_lease):
    """A PUT sends the frozen fields back too. Amendments are partial."""
    response = _client(landlord).put(
        f"/api/leases/{bc_lease.pk}/",
        {"special_terms": "Amended."},
        format="json",
    )
    assert response.status_code == 403


def test_rama_and_the_ui_agree_about_what_is_amendable(landlord, bc_lease):
    """Two doors, one rule. RAMA used to refuse every ACTIVE lease outright;
    a landlord told 'yes' by the dashboard and 'no' by RAMA on the same edit
    is the inconsistency this pair of checks exists to prevent."""
    from rentium.rama import registry

    result = registry.execute(
        "update_lease",
        {
            "lease_number": bc_lease.lease_number,
            "special_terms": "Amended through RAMA.",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert not result.get("error"), result
    bc_lease.refresh_from_db()
    assert bc_lease.special_terms == "Amended through RAMA."
