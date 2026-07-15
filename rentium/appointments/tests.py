"""Appointment visibility, the public status link, and event fan-out.

The visibility rules are the contract that keeps a landlord's showing
pipeline out of an incoming tenant's calendar — see
AppointmentViewSet.get_queryset for the statement of the rule.
"""

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from rentium.appointments.models import Appointment
from rentium.events.tasks import process_domain_event
from rentium.users.tests.factories import UserFactory


@pytest.fixture
def tenant_on_lease(tenant, bc_lease):
    from rentium.leases.models import LeaseTenant

    LeaseTenant.objects.create(
        lease=bc_lease,
        tenant=tenant,
        rent_amount="850.00",
        is_primary_tenant=True,
        has_signed=True,
    )
    return tenant


def _client_for(profile):
    client = APIClient()
    client.force_authenticate(user=profile.user)
    return client


def _appt(landlord, prop, *, starts_at, lease=None, status=Appointment.Status.SCHEDULED, **kw):
    return Appointment.objects.create(
        landlord=landlord,
        property=prop,
        lease=lease,
        kind=Appointment.Kind.VIEWING,
        status=status,
        starts_at=starts_at,
        **kw,
    )


# ------------------------------------------------------------- visibility
@pytest.mark.django_db
def test_tenant_never_sees_pre_tenancy_showings(
    landlord, bc_property, bc_lease, tenant_on_lease
):
    """Property-wide viewings from BEFORE the lease started (the landlord
    showing the unit to other prospects) must not leak to the new tenant."""
    before_move_in = timezone.now() - timedelta(days=3)  # lease starts today
    leaked = _appt(landlord, bc_property, starts_at=before_move_in)

    during_tenancy = timezone.now() + timedelta(days=2)
    entry_notice = _appt(landlord, bc_property, starts_at=during_tenancy)

    on_my_lease = _appt(
        landlord, bc_property, starts_at=during_tenancy, lease=bc_lease
    )

    res = _client_for(tenant_on_lease).get("/api/appointments/")
    assert res.status_code == 200
    ids = {a["id"] for a in res.data}
    assert str(leaked.pk) not in ids
    assert str(entry_notice.pk) in ids
    assert str(on_my_lease.pk) in ids


@pytest.mark.django_db
def test_tenant_never_sees_requested_or_cancelled(
    landlord, bc_property, tenant_on_lease
):
    when = timezone.now() + timedelta(days=1)
    _appt(landlord, bc_property, starts_at=when, status=Appointment.Status.REQUESTED)
    _appt(landlord, bc_property, starts_at=when, status=Appointment.Status.CANCELLED)

    res = _client_for(tenant_on_lease).get("/api/appointments/")
    assert res.status_code == 200
    assert res.data == []


@pytest.mark.django_db
def test_tenant_sees_visit_without_prospect_contact_details(
    landlord, bc_property, tenant_on_lease
):
    """Who is coming and when = the tenant's entry notice. The visitor's
    email/phone = the landlord's business."""
    appt = _appt(
        landlord,
        bc_property,
        starts_at=timezone.now() + timedelta(days=1),
        contact_name="Prospect Pat",
        contact_email="pat@example.com",
        contact_phone="250-555-0000",
    )

    res = _client_for(tenant_on_lease).get("/api/appointments/")
    row = next(a for a in res.data if a["id"] == str(appt.pk))
    assert row["contact_name"] == "Prospect Pat"
    assert row["contact_email"] == ""
    assert row["contact_phone"] == ""

    res = _client_for(landlord).get("/api/appointments/")
    row = next(a for a in res.data if a["id"] == str(appt.pk))
    assert row["contact_email"] == "pat@example.com"


# ------------------------------------------------------- public status link
@pytest.mark.django_db
def test_public_status_endpoint_reads_one_appointment_by_token(
    landlord, bc_property
):
    appt = _appt(
        landlord,
        bc_property,
        starts_at=timezone.now() + timedelta(days=1),
        status=Appointment.Status.REQUESTED,
        contact_name="Prospect Pat",
        contact_email="pat@example.com",
    )
    client = APIClient()  # anonymous

    res = client.get(f"/api/public/viewing-status/{appt.public_token}/")
    assert res.status_code == 200
    assert res.data["status"] == "REQUESTED"
    assert res.data["property"]["name"] == bc_property.name
    # privacy: never the street address
    assert "1234 Oak Ave" not in str(res.data)

    import uuid

    res = client.get(f"/api/public/viewing-status/{uuid.uuid4()}/")
    assert res.status_code == 404


# ---------------------------------------------------------------- fan-out
@pytest.mark.django_db
def test_requested_event_notifies_landlord_and_emails_prospect(
    landlord, bc_property
):
    appt = _appt(
        landlord,
        bc_property,
        starts_at=timezone.now() + timedelta(days=1),
        status=Appointment.Status.REQUESTED,
        contact_name="Prospect Pat",
        contact_email="pat@example.com",
    )
    event = appt.publish_event("appointment.requested")
    process_domain_event(str(event.id))

    from rentium.events.models import Notification

    n = Notification.objects.get(recipient=landlord.user)
    assert bc_property.name in n.title

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["pat@example.com"]
    assert f"/viewing/status/{appt.public_token}" in mail.outbox[0].body


@pytest.mark.django_db
def test_scheduled_event_emails_prospect_and_notifies_lease_tenants(
    landlord, bc_property, bc_lease, tenant_on_lease
):
    appt = _appt(
        landlord,
        bc_property,
        starts_at=timezone.now() + timedelta(days=1),
        lease=bc_lease,
        contact_name="Prospect Pat",
        contact_email="pat@example.com",
    )
    event = appt.publish_event("appointment.scheduled")
    process_domain_event(str(event.id))

    from rentium.events.models import Notification

    assert Notification.objects.filter(recipient=tenant_on_lease.user).exists()
    assert len(mail.outbox) == 1
    assert "Confirmed" in mail.outbox[0].subject


@pytest.mark.django_db
def test_landlord_confirm_flow_end_to_end(landlord, bc_property):
    """Public request -> landlord confirm -> status page shows SCHEDULED."""
    from rentium.showcase.models import Showcase

    # The public() gate: showcase opted in + visible + AVAILABLE.
    showcase, _ = Showcase.objects.get_or_create(landlord=landlord)
    showcase.slug = "oak-ave-rentals"
    showcase.is_public = True
    showcase.save()
    bc_property.is_publicly_visible = True
    bc_property.status = "AVAILABLE"
    bc_property.save()

    anon = APIClient()
    res = anon.post(
        "/api/public/viewing-requests/",
        {
            "property": bc_property.public_slug or bc_property.pk,
            "name": "Prospect Pat",
            "email": "pat@example.com",
            "requested_time": (timezone.now() + timedelta(days=2)).isoformat(),
        },
        format="json",
    )
    assert res.status_code == 201
    token = res.data["status_token"]

    appt = Appointment.objects.get(public_token=token)
    res = _client_for(landlord).post(f"/api/appointments/{appt.pk}/confirm/")
    assert res.status_code == 200

    res = anon.get(f"/api/public/viewing-status/{token}/")
    assert res.data["status"] == "SCHEDULED"
