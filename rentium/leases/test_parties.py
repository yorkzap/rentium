from __future__ import annotations

from datetime import date

import pytest

from rentium.leases.models import Lease
from rentium.leases.models import LeaseLandlordSignatory
from rentium.leases.models import LeaseTenant
from rentium.leases.parties import lease_signing_fact_packet
from rentium.leases.parties import lease_signing_roster
from rentium.users.models import TenantProfile
from rentium.users.tests.factories import UserFactory

EXPECTED_PARTY_COUNT = 4

pytestmark = pytest.mark.django_db


def _signing_lease(landlord, bc_property):
    return Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date(2026, 9, 1),
        is_month_to_month=True,
        total_rent="2000.00",
        lease_number="RMT652523-C281",
        landlord_signed=True,
    )


def test_roster_includes_owner_co_landlord_and_every_active_tenant(
    landlord, bc_property,
):
    lease = _signing_lease(landlord, bc_property)
    co_landlord = LeaseLandlordSignatory.objects.create(
        lease=lease,
        name="Sarbjeet Kaur",
        email="sarbjeet@example.com",
        has_signed=False,
    )
    first = LeaseTenant.objects.create(
        lease=lease,
        tenant=TenantProfile.objects.create(
            user=UserFactory(name="Aiswarya Chenthamara"),
        ),
        invited_email="aiswarya@example.com",
        rent_amount="1000.00",
        is_primary_tenant=False,
    )
    second = LeaseTenant.objects.create(
        lease=lease,
        tenant=TenantProfile.objects.create(
            user=UserFactory(name="Naveen Prasanth Singaravel"),
        ),
        invited_email="naveen@example.com",
        rent_amount="1000.00",
        is_primary_tenant=True,
    )

    roster = lease_signing_roster(lease)

    assert [row["role"] for row in roster["parties"]] == [
        "LANDLORD",
        "CO_LANDLORD",
        "TENANT",
        "TENANT",
    ]
    assert [row["name"] for row in roster["parties"]] == [
        landlord.user.name,
        "Sarbjeet Kaur",
        "Aiswarya Chenthamara",
        "Naveen Prasanth Singaravel",
    ]
    assert roster["expected_signer_count"] == EXPECTED_PARTY_COUNT
    assert {row["id"] for row in roster["unsigned_expected_signers"]} == {
        str(co_landlord.pk),
        str(first.pk),
        str(second.pk),
    }
    assert roster["signature_activation_blockers"] == [
        {
            "kind": "party_signature",
            "party_id": str(co_landlord.pk),
            "role": "CO_LANDLORD",
            "name": "Sarbjeet Kaur",
        },
        {
            "kind": "tenant_signature_requirement",
            "requirement": "AT_LEAST_ONE_TENANT",
            "eligible_party_ids": [str(first.pk), str(second.pk)],
        },
    ]


def test_one_tenant_signature_satisfies_group_but_other_tenant_stays_expected(
    landlord, bc_property,
):
    lease = _signing_lease(landlord, bc_property)
    signed = LeaseTenant.objects.create(
        lease=lease,
        tenant=TenantProfile.objects.create(user=UserFactory(name="Signed Tenant")),
        invited_email="signed@example.com",
        rent_amount="1000.00",
        has_signed=True,
    )
    unsigned = LeaseTenant.objects.create(
        lease=lease,
        tenant=TenantProfile.objects.create(user=UserFactory(name="Unsigned Tenant")),
        invited_email="unsigned@example.com",
        rent_amount="1000.00",
    )

    roster = lease_signing_roster(lease)

    assert roster["activation_requirements"]["at_least_one_tenant_signed"] is True
    assert roster["signature_requirements_satisfied"] is True
    assert [row["id"] for row in roster["unsigned_expected_signers"]] == [
        str(unsigned.pk),
    ]
    tenant_rows = {row["id"]: row for row in roster["tenants"]}
    assert tenant_rows[str(signed.pk)]["required_individually_for_activation"] is False
    assert tenant_rows[str(unsigned.pk)]["expected_to_sign"] is True


def test_fact_packet_allows_absence_only_from_complete_roster(landlord, bc_property):
    lease = _signing_lease(landlord, bc_property)
    LeaseTenant.objects.create(
        lease=lease,
        tenant=TenantProfile.objects.create(user=UserFactory(name="Signed Tenant")),
        invited_email="signed@example.com",
        rent_amount="2000.00",
        has_signed=True,
    )

    packet = lease_signing_fact_packet(lease)

    assert packet.coverage.value == "COMPLETE"
    assert packet.permits_negative("no_co_landlords") is True
    assert packet.permits_negative("no_unsigned_expected_signers") is True
    assert packet.to_dict()["facet"] == "lease.signing_roster"


def test_document_signature_section_uses_canonical_roster(landlord, bc_property):
    from rentium.leases.documents import signature_rows

    lease = _signing_lease(landlord, bc_property)
    LeaseLandlordSignatory.objects.create(
        lease=lease,
        name="Sarbjeet Kaur",
        email="sarbjeet@example.com",
    )
    LeaseTenant.objects.create(
        lease=lease,
        tenant=TenantProfile.objects.create(user=UserFactory(name="A Tenant")),
        invited_email="tenant@example.com",
        rent_amount="2000.00",
    )

    labels = [row.label for row in signature_rows(lease)]

    assert labels == [
        f"Landlord — {landlord.user.name}",
        "Co-landlord — Sarbjeet Kaur",
        "Tenant — A Tenant",
    ]
