from __future__ import annotations

import uuid
from datetime import date
from unittest import mock

import pytest
from rest_framework.test import APIClient

from rentium.leases.models import Lease
from rentium.leases.models import LeaseLandlordSignatory
from rentium.leases.models import LeaseTenant
from rentium.properties.models import Property
from rentium.rama.models import RamaPreferences
from rentium.rama.service import run_turn
from rentium.users.models import TenantProfile
from rentium.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
HTTP_OK = 200


def _rama_failure_lease(landlord):
    landlord.user.name = "Raj Singh"
    landlord.user.save(update_fields=["name"])
    prop = Property.objects.create(
        landlord=landlord,
        name="Garden Suite",
        address="940 McKenzie Avenue",
        city="Victoria",
        province="BC",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    )
    lease = Lease.objects.create(
        landlord=landlord,
        property=prop,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date(2026, 9, 1),
        is_month_to_month=True,
        total_rent="2000.00",
        lease_number="RMT652523-C281",
        landlord_signed=True,
    )
    LeaseLandlordSignatory.objects.create(
        lease=lease,
        name="Sarbjeet Kaur",
        email="sarbjeet@example.com",
        has_signed=False,
    )
    LeaseTenant.objects.create(
        lease=lease,
        tenant=TenantProfile.objects.create(
            user=UserFactory(name="Aiswarya Chenthamara"),
        ),
        invited_email="aiswarya@example.com",
        rent_amount="1000.00",
    )
    LeaseTenant.objects.create(
        lease=lease,
        tenant=TenantProfile.objects.create(
            user=UserFactory(name="Naveen Prasanth Singaravel"),
        ),
        invited_email="naveen@example.com",
        rent_amount="1000.00",
        is_primary_tenant=True,
    )
    prefs = RamaPreferences.for_landlord(landlord)
    prefs.web_orchestration_mode = RamaPreferences.OrchestrationMode.V3
    prefs.save(update_fields=["web_orchestration_mode", "updated_at"])
    return lease


def test_v3_replays_signer_failure_without_provider_or_false_absence(landlord):
    _rama_failure_lease(landlord)
    conversation_id = uuid.uuid4()

    with mock.patch("rentium.rama.service.get_provider") as get_provider:
        identity = run_turn(
            landlord,
            "what is aishwarya and naveen's lease number?",
            conversation_id,
            visible_text="what is aishwarya and naveen's lease number?",
        )
        unsigned = run_turn(
            landlord,
            "who hasn't signed it yet and name all parties supposed to sign",
            conversation_id,
            visible_text=(
                "who hasn't signed it yet and name all parties supposed to sign"
            ),
        )
        landlords = run_turn(
            landlord,
            "what about the landlords on that lease?",
            conversation_id,
            visible_text="what about the landlords on that lease?",
        )

    get_provider.assert_not_called()
    assert "RMT652523-C281" in identity.reply
    assert identity.answer_mode == "CERTIFIED"
    assert identity.coverage == "COMPLETE"

    assert "Raj Singh — Landlord — signed" in unsigned.reply
    assert "Sarbjeet Kaur — Co-landlord — not signed" in unsigned.reply
    assert "Aiswarya Chenthamara" in unsigned.reply
    assert "Naveen Prasanth Singaravel" in unsigned.reply
    assert unsigned.evidence["facet"] == "lease.signing_roster"

    assert "Raj Singh — Landlord — signed" in landlords.reply
    assert "Sarbjeet Kaur — Co-landlord — not signed" in landlords.reply
    assert "not recorded" not in landlords.reply.casefold()
    assert landlords.engine_mode == "v3"
    assert landlords.trace_id is not None


def test_v3_abstains_with_exact_resolution_gap(landlord):
    prefs = RamaPreferences.for_landlord(landlord)
    prefs.web_orchestration_mode = RamaPreferences.OrchestrationMode.V3
    prefs.save(update_fields=["web_orchestration_mode", "updated_at"])

    result = run_turn(
        landlord,
        "what is Unknown Person's lease number?",
        uuid.uuid4(),
        visible_text="what is Unknown Person's lease number?",
    )

    assert result.answer_mode == "ABSTAINED"
    assert result.coverage == "PARTIAL"
    assert "could not resolve" in result.reply
    assert "not on a lease" not in result.reply


def test_v3_chat_api_returns_certified_metadata_without_provider_key(
    landlord,
    settings,
):
    lease = _rama_failure_lease(landlord)
    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.api_key = ""
    prefs.save(update_fields=["enabled", "api_key", "updated_at"])
    settings.XAI_API_KEY = ""
    settings.ANTHROPIC_API_KEY = ""
    settings.OPENAI_API_KEY = ""
    client = APIClient()
    client.force_authenticate(user=landlord.user)

    with mock.patch("rentium.rama.service.get_provider") as get_provider:
        response = client.post(
            "/api/rama/chat/",
            {"message": f"Who has signed lease {lease.lease_number}?"},
            format="json",
        )

    assert response.status_code == HTTP_OK
    assert response.json()["answer_mode"] == "CERTIFIED"
    assert response.json()["coverage"] == "COMPLETE"
    assert response.json()["engine_mode"] == "v3"
    assert response.json()["trace_id"]
    get_provider.assert_not_called()
