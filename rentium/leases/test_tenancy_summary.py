from datetime import date, timedelta
from unittest.mock import patch
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.test import APIClient

from rentium.properties.models import (
    Property,
    PropertyArea,
    PropertyHolding,
    PropertyUnit,
)
from rentium.properties.configuration_services import (
    confirm_configuration,
    preview_configuration,
)
from rentium.rama.service import run_turn
from rentium.rama.test_evidence_core import _rama_failure_lease

from .models import Lease, MoveOutRequest
from .space_services import record_space_scope
from .tenancy_state import TenancyAssessment
from .tenancy_summary import tenancy_summary

pytestmark = pytest.mark.django_db


def test_reported_notice_conversation_without_provider(landlord):
    lease = _rama_failure_lease(landlord)
    lease.lease_tenants.update(has_signed=True)
    MoveOutRequest.objects.create(
        lease=lease,
        kind="TENANT_NOTICE",
        initiated_by="TENANT",
        status="ACCEPTED",
        requested_end_date=date(2026, 10, 31),
        effective_end_date=date(2026, 10, 31),
        rent_handling="NONE",
    )
    conversation = uuid.uuid4()
    with patch("rentium.rama.service.get_provider") as provider:
        first = run_turn(
            landlord, "is mckenzie garden suite rented out rn?", conversation
        )
        followup = run_turn(
            landlord, "i thought they sent a move out notice ?", conversation
        )
        correction = run_turn(
            landlord,
            "they sent this Tenant Notice to End Tenancy Accepted",
            conversation,
        )
    provider.assert_not_called()
    assert "2026-10-31" in first.reply
    assert "don't confirm actual occupancy" in first.reply
    assert "Sarbjeet" in first.reply
    assert "accepted" in followup.reply
    assert "accepted" in correction.reply
    assert followup.details["notices"][0]["rent_handling"] == "NONE"
    assert followup.sources[0]["url"].endswith(str(lease.pk))
    assert len(followup.reply.split()) < 85


def test_notice_absence_is_scoped_and_not_document_absence(landlord):
    lease = _rama_failure_lease(landlord)
    result = run_turn(
        landlord,
        f"Did they send a tenant notice for {lease.lease_number}?",
        uuid.uuid4(),
    )
    assert "notice history" in result.reply
    assert "outside Rentium" in result.reply


def test_lease_api_and_projection_agree(landlord):
    lease = _rama_failure_lease(landlord)
    client = APIClient()
    client.force_authenticate(landlord.user)
    response = client.get(f"/api/leases/{lease.pk}/")
    assert response.status_code == 200
    assert (
        response.data["tenancy_summary"]["occupancy"]
        == tenancy_summary(lease)["occupancy"]
    )


def unit_fixture(landlord):
    holding = PropertyHolding.objects.create(
        landlord=landlord, name="Home", address="950 McKenzie Ave"
    )
    unit = PropertyUnit.objects.create(
        landlord=landlord,
        holding=holding,
        name="Garden Suite",
        rental_mode="WHOLE_UNIT",
    )
    common = dict(
        landlord=landlord,
        unit=unit,
        holding=holding,
        address="950 McKenzie Ave",
        province="BC",
        city="Victoria",
    )
    whole = Property.objects.create(
        **common, name="Garden Suite", property_category="COMPLETE_UNIT"
    )
    rooms, areas = [], []
    for name in ["K", "J"]:
        room = Property.objects.create(
            **common, name=name, property_category="ROOM", is_active_offering=False
        )
        areas.append(
            PropertyArea.objects.create(
                unit=unit, name=name, area_type="BEDROOM", exclusive_to=room
            )
        )
        rooms.append(room)
    return unit, whole, rooms, areas


def test_whole_scope_blocks_room_bundles_and_keeps_history(landlord):
    unit, whole, rooms, areas = unit_fixture(landlord)
    lease = Lease.objects.create(
        landlord=landlord,
        property=whole,
        lease_type="BC_RESIDENTIAL",
        start_date=date.today(),
        is_month_to_month=True,
    )
    scope = record_space_scope(
        lease,
        unit=unit,
        whole_unit=True,
        room_ids=[],
        user=landlord.user,
        reason="Confirmed whole suite",
    )
    with pytest.raises(ValidationError, match="overlaps"):
        Lease.objects.create(
            landlord=landlord,
            property=rooms[0],
            lease_type="GENERIC_ROOMMATE",
            start_date=date.today(),
            is_month_to_month=True,
        )
    with pytest.raises(ValidationError, match="Append"):
        scope.save()


def test_future_configuration_requires_notice_and_detects_stale_preview(landlord):
    unit, whole, rooms, areas = unit_fixture(landlord)
    today = timezone.localdate()
    lease = Lease.objects.create(
        landlord=landlord,
        property=whole,
        lease_type="BC_RESIDENTIAL",
        start_date=today,
        is_month_to_month=True,
    )
    end = today + timedelta(days=20)
    blocked = preview_configuration(
        unit, mode="BY_ROOM", effective_from=end + timedelta(days=1)
    )
    assert not blocked["ok"]
    MoveOutRequest.objects.create(
        lease=lease,
        kind="TENANT_NOTICE",
        initiated_by="TENANT",
        status="ACCEPTED",
        requested_end_date=end,
        effective_end_date=end,
    )
    preview = preview_configuration(
        unit, mode="BY_ROOM", effective_from=end + timedelta(days=1)
    )
    assert preview["ok"]
    receipt = confirm_configuration(
        unit, preview_token=preview["preview_token"], user=landlord.user
    )
    assert receipt["status"] == "SCHEDULED"
    assert (
        confirm_configuration(
            unit, preview_token=preview["preview_token"], user=landlord.user
        )
        == receipt
    )
    unit.refresh_from_db()
    assert unit.rental_mode == "WHOLE_UNIT"
    assert whole.pk in unit.offerings.filter(is_active_offering=True).values_list(
        "pk", flat=True
    )


def test_unrelated_sharing_does_not_create_bc_exemption(landlord):
    from .tenancy_rules import rules_for_lease

    lease = _rama_failure_lease(landlord)
    lease.property.property_category = "ROOM"
    lease.property.save()
    lease.property.property_category = "ROOM"
    lease.property.save()
    lease.lease_type = "GENERIC_ROOMMATE"
    lease.common_space_shared_with = ["LANDLORD_RELATIVES", "LANDLORD"]
    lease.save()
    assert rules_for_lease(lease).rta_applies
    assert tenancy_summary(lease)["assessment"]["decision"] == "REVIEW"
    with pytest.raises(ValidationError):
        TenancyAssessment.objects.create(
            lease=lease,
            jurisdiction="BC",
            decision="EXEMPT_OWNER_SHARED",
            facts={"facilities": ["LAUNDRY"]},
            reason="Shared laundry",
        )
    TenancyAssessment.objects.create(
        lease=lease,
        jurisdiction="BC",
        decision="EXEMPT_OWNER_SHARED",
        facts={
            "facilities": ["KITCHEN"],
            "owner_identity": "Owner",
            "owner_confirmed": True,
            "shared_at_tenancy_start": True,
        },
        reason="Owner-confirmed sharing",
    )
    assert not rules_for_lease(lease).rta_applies


def test_disjoint_rooms_and_bundles_use_physical_space(landlord):
    unit, whole, rooms, areas = unit_fixture(landlord)
    first = Lease.objects.create(
        landlord=landlord,
        property=rooms[0],
        lease_type="GENERIC_ROOMMATE",
        start_date=timezone.localdate(),
        is_month_to_month=True,
    )
    record_space_scope(
        first,
        unit=unit,
        whole_unit=False,
        room_ids=[areas[0].pk],
        user=landlord.user,
        reason="Room K only",
    )
    second = Lease.objects.create(
        landlord=landlord,
        property=rooms[1],
        lease_type="GENERIC_ROOMMATE",
        start_date=timezone.localdate(),
        is_month_to_month=True,
    )
    record_space_scope(
        second,
        unit=unit,
        whole_unit=False,
        room_ids=[areas[1].pk],
        user=landlord.user,
        reason="Room J only",
    )
    with pytest.raises(ValidationError, match="overlaps"):
        record_space_scope(
            second,
            unit=unit,
            whole_unit=False,
            room_ids=[a.pk for a in areas],
            user=landlord.user,
            reason="Cannot bundle a committed room",
        )


def test_configuration_preview_is_stale_after_layout_change(landlord):
    unit, whole, rooms, areas = unit_fixture(landlord)
    preview = preview_configuration(
        unit, mode="BY_ROOM", effective_from=timezone.localdate()
    )
    PropertyArea.objects.filter(pk=areas[0].pk).update(exclusive_to=None)
    with pytest.raises(ValidationError, match="changed"):
        confirm_configuration(
            unit, preview_token=preview["preview_token"], user=landlord.user
        )
    assert not unit.rental_configurations.exists()


def test_missing_whole_listing_is_created_once_and_not_published(landlord):
    unit, whole, rooms, areas = unit_fixture(landlord)
    # An unrelated historical room offering must not be relabelled as a whole unit.
    whole.property_category = "ROOM"
    whole.save()
    preview = preview_configuration(
        unit, mode="WHOLE_UNIT", effective_from=timezone.localdate()
    )
    assert preview["will_create"][0]["name"] == unit.name
    result = confirm_configuration(
        unit, preview_token=preview["preview_token"], user=landlord.user
    )
    repeat = confirm_configuration(
        unit, preview_token=preview["preview_token"], user=landlord.user
    )
    assert repeat == result
    assert result["status"] == "ACTIVE"
    created = unit.offerings.get(property_category="COMPLETE_UNIT")
    assert not created.is_publicly_visible
    assert unit.offerings.count() == 4
    assert unit.areas.filter(is_seeded_default=False).count() == 2


def test_review_records_possession_only_after_confirmation(landlord):
    from .tenancy_review import preview_tenancy_review, confirm_tenancy_review
    from .space_services import occupancy_state

    lease = _rama_failure_lease(landlord)
    preview = preview_tenancy_review(
        lease,
        presence={"state": "DEPARTED", "occurred_on": timezone.localdate().isoformat()},
        reason="All keys returned and unit checked",
    )
    assert occupancy_state(lease) == "UNCONFIRMED"
    result = confirm_tenancy_review(
        lease, preview_token=preview["preview_token"], user=landlord.user
    )
    assert result["occupancy"] == "RECORDED_DEPARTED"
    confirm_tenancy_review(
        lease, preview_token=preview["preview_token"], user=landlord.user
    )
    assert lease.presence_events.count() == 1
    with pytest.raises(ValidationError, match="future"):
        preview_tenancy_review(
            lease,
            presence={
                "state": "DEPARTED",
                "occurred_on": (timezone.localdate() + timedelta(days=1)).isoformat(),
            },
            reason="Planned only",
        )


def test_review_keeps_signed_terms_and_notice_unchanged(landlord):
    from .tenancy_review import preview_tenancy_review, confirm_tenancy_review

    unit, whole, rooms, areas = unit_fixture(landlord)
    lease = Lease.objects.create(
        landlord=landlord,
        property=rooms[0],
        lease_type="GENERIC_ROOMMATE",
        start_date=timezone.localdate(),
        is_month_to_month=True,
        special_terms="Signed terms preserved",
    )
    preview = preview_tenancy_review(
        lease,
        scope={"whole_unit": True, "room_ids": []},
        assessment={"decision": "APPLIES", "facts": {"shared_at_tenancy_start": False}},
        reason="Confirmed this agreement covers the entire suite",
    )
    result = confirm_tenancy_review(
        lease, preview_token=preview["preview_token"], user=landlord.user
    )
    lease.refresh_from_db()
    assert lease.special_terms == "Signed terms preserved"
    assert lease.property == rooms[0]
    assert result["scope"]["whole_unit"]
    assert result["assessment"]["decision"] == "APPLIES"


def test_future_term_is_not_occupancy_and_whole_scope_covers_room_offerings(landlord):
    from rentium.rama.union import occupancy_as_of

    unit, whole, rooms, areas = unit_fixture(landlord)
    for room in rooms:
        room.is_active_offering = True
        room.save()
    lease = Lease.objects.create(
        landlord=landlord,
        property=whole,
        lease_type="BC_RESIDENTIAL",
        status="PENDING",
        start_date=timezone.localdate() + timedelta(days=10),
        is_month_to_month=True,
    )
    response = occupancy_as_of(
        landlord, (timezone.localdate() + timedelta(days=20)).isoformat()
    )
    for row in response["listings"]:
        assert not row["occupied_on_date"]
        assert not row["vacant_on_date"]
        assert row["lease"]["lease_id"] == str(lease.pk)


def test_unit_review_api_rejects_tenant_and_other_owner(landlord, tenant):
    lease = _rama_failure_lease(landlord)
    client = APIClient()
    client.force_authenticate(tenant.user)
    response = client.post(
        f"/api/leases/{lease.pk}/tenancy_review/",
        {
            "presence": {"state": "PRESENT", "occurred_on": str(timezone.localdate())},
            "reason": "Not authorized",
        },
        format="json",
    )
    assert response.status_code in {403, 404}
    assert not lease.presence_events.exists()


def test_rama_dated_configuration_uses_preview_and_verified_receipt(landlord):
    from rentium.rama import registry
    from rentium.rama.workflow_contracts import verify

    unit, whole, rooms, areas = unit_fixture(landlord)
    args = {
        "unit_name": str(unit.pk),
        "rental_mode": "rooms",
        "effective_from": str(timezone.localdate() + timedelta(days=10)),
    }
    preview = registry.execute("set_unit_rental_mode", args, landlord=landlord)
    assert preview["needs_confirm"]
    assert not unit.rental_configurations.exists()
    args.update(preview["resolved_arguments"])
    receipt = registry.execute(
        "set_unit_rental_mode", {**args, "confirm": "yes"}, landlord=landlord
    )
    assert receipt["status"] == "SCHEDULED"
    assert verify("set_unit_rental_mode", args, receipt, landlord)["verified"]


def test_unknown_named_unit_does_not_reuse_previous_tenancy(landlord):
    from rentium.rama.tenancy_reads import resolve_tenancy

    lease = _rama_failure_lease(landlord)
    resolved, choices = resolve_tenancy(
        landlord,
        "is Unknown Suite rented out rn?",
        {"client_context": {"lease_id": str(lease.pk)}},
    )
    assert resolved is None


def test_continuation_and_blocked_configuration_surface_in_attention(landlord):
    from rentium.attention.service import _tenancy_reviews
    from rentium.properties.rental_configuration import RentalConfiguration

    unit, whole, rooms, areas = unit_fixture(landlord)
    lease = Lease.objects.create(
        landlord=landlord,
        property=whole,
        lease_type="BC_RESIDENTIAL",
        status="ACTIVE",
        start_date=timezone.localdate() - timedelta(days=60),
        end_date=timezone.localdate() - timedelta(days=1),
        total_rent="1000",
    )
    RentalConfiguration.objects.create(
        unit=unit,
        mode="BY_ROOM",
        effective_from=timezone.localdate(),
        status="BLOCKED",
        offering_ids=[r.pk for r in rooms],
        receipt_key="attention-test",
        blocked_reason="Departure needs confirmation",
    )
    items = _tenancy_reviews(landlord)
    assert len(items) == 2
    assert {item.severity for item in items} == {"urgent"}
    assert str(lease.pk) in items[0].url
