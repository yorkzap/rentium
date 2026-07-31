"""Prospect viewing-status link open tracking."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from rentium.appointments.models import Appointment
from rentium.properties.models import Property

pytestmark = pytest.mark.django_db


def _listing(landlord):
    return Property.objects.create(
        landlord=landlord,
        name="Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )


def _viewing(landlord, prop, **kwargs):
    defaults = {
        "landlord": landlord,
        "property": prop,
        "kind": Appointment.Kind.VIEWING,
        "status": Appointment.Status.SCHEDULED,
        "starts_at": timezone.now() + timedelta(days=1),
        "contact_name": "Ishupreet Sidhu",
        "contact_email": "ishu@example.com",
    }
    defaults.update(kwargs)
    return Appointment.objects.create(**defaults)


def test_public_status_page_records_open(landlord, client):
    prop = _listing(landlord)
    appt = _viewing(landlord, prop)
    assert appt.prospect_link_open_count == 0

    res = client.get(f"/api/public/viewing-status/{appt.public_token}/")
    assert res.status_code == 200
    appt.refresh_from_db()
    assert appt.prospect_link_open_count == 1
    assert appt.prospect_link_first_opened_at is not None
    assert appt.prospect_link_last_opened_at is not None

    client.get(f"/api/public/viewing-status/{appt.public_token}/")
    appt.refresh_from_db()
    assert appt.prospect_link_open_count == 2


def test_viewing_invite_status_tool(landlord):
    from rentium.rama import registry

    prop = _listing(landlord)
    appt = _viewing(landlord, prop)
    before = registry.execute(
        "viewing_invite_status",
        {"contact": "Ishupreet"},
        landlord=landlord,
    )
    assert before.get("ok") is True
    assert before.get("link_opened") is False

    appt.record_prospect_link_open()
    after = registry.execute(
        "viewing_invite_status",
        {"contact": "ishu@example.com"},
        landlord=landlord,
    )
    assert after.get("link_opened") is True
    assert after.get("open_count") >= 1
    assert "opened" in (after.get("message") or "").casefold()


def test_have_they_seen_maps_to_tool():
    from rentium.rama.capabilities import supported_tool_for_request

    assert (
        supported_tool_for_request("have they seen the viewing link?")
        == "viewing_invite_status"
    )
