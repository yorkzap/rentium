"""Reschedule an existing viewing — the gap that made "change Hitakshi to Aug 4" fail."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.core import mail

from rentium.appointments.models import Appointment
from rentium.events.tasks import process_domain_event
from rentium.properties.models import Property
from rentium.rama import registry

pytestmark = pytest.mark.django_db

TZ = ZoneInfo("America/Vancouver")


@pytest.fixture
def room(landlord):
    return Property.objects.create(
        landlord=landlord,
        name="Room D",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )


def _scheduled(landlord, room, when):
    return Appointment.objects.create(
        landlord=landlord,
        property=room,
        kind=Appointment.Kind.VIEWING,
        status=Appointment.Status.SCHEDULED,
        starts_at=when,
        contact_name="Hikashi Verma",
        contact_email="Hitakshiverma01@gmail.com",
    )


def test_reschedule_preview_and_confirm_moves_time_and_emails(landlord, room):
    appt = _scheduled(
        landlord,
        room,
        datetime(2026, 7, 31, 14, 0, tzinfo=TZ),
    )

    preview = registry.execute(
        "reschedule_viewing",
        {
            "appointment_ref": str(appt.pk),
            "when": "2026-08-04 14:00",
        },
        landlord=landlord,
    )
    assert preview.get("needs_confirm"), preview
    assert "August" in preview["preview"]["to"] or "2026-08-04" in preview["preview"]["to"]
    appt.refresh_from_db()
    assert appt.starts_at.day == 31  # not written yet

    done = registry.execute(
        "reschedule_viewing",
        {
            "appointment_ref": str(appt.pk),
            "when": "2026-08-04 14:00",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("rescheduled"), done
    appt.refresh_from_db()
    local = appt.starts_at.astimezone(TZ)
    assert (local.year, local.month, local.day, local.hour) == (2026, 8, 4, 14)

    from rentium.events.models import DomainEvent

    event = DomainEvent.objects.filter(
        event_type="appointment.rescheduled",
        payload__appointment_id=str(appt.pk),
    ).latest("created_at")
    process_domain_event(str(event.id))
    assert any(
        "Hitakshiverma01@gmail.com" in m.to and "Updated" in m.subject
        for m in mail.outbox
    )


def test_reschedule_by_contact_and_property(landlord, room):
    _scheduled(
        landlord,
        room,
        datetime(2026, 7, 31, 14, 0, tzinfo=TZ),
    )
    done = registry.execute(
        "reschedule_viewing",
        {
            "property_query": "Room D",
            "contact": "Hitakshiverma01@gmail.com",
            "when": "2026-08-04 14:00",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("rescheduled"), done


def test_log_capability_gap_refuses_reschedule(landlord):
    out = registry.execute(
        "log_capability_gap",
        {
            "request": "reschedule the viewing from July 31 to August 4 at 2pm",
        },
        landlord=landlord,
    )
    assert out.get("logged") is False
    assert out.get("tool") == "reschedule_viewing"


def test_reschedule_same_slot_is_already_done_not_an_error(landlord, room):
    appt = _scheduled(
        landlord,
        room,
        datetime(2026, 8, 4, 14, 0, tzinfo=TZ),
    )
    out = registry.execute(
        "reschedule_viewing",
        {
            "appointment_ref": str(appt.pk),
            "when": "2026-08-04 14:00",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert out.get("already_done") is True
    assert out.get("rescheduled") is False
    assert "error" not in out
    appt.refresh_from_db()
    assert appt.starts_at.astimezone(TZ).day == 4
