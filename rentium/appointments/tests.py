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


# --------------------------------------------------- availability windows
@pytest.mark.django_db
def test_classify_time_in_and_out_of_hours(landlord, bc_property):
    """A time inside a preferred window is IN_HOURS; outside is OUT_OF_HOURS;
    no windows at all is UNSET."""
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from rentium.appointments.models import AvailabilityWindow, Weekday
    from rentium.appointments import services

    tz = ZoneInfo("America/Vancouver")

    # No windows configured yet.
    a_tuesday = datetime(2026, 8, 4, 18, 0, tzinfo=tz)  # 2026-08-04 is a Tuesday
    assert services.classify_time(landlord, bc_property, a_tuesday) == services.UNSET

    # Landlord default: Tuesdays 17:00–19:00.
    AvailabilityWindow.objects.create(
        landlord=landlord,
        weekday=Weekday.TUESDAY,
        start_time=time(17, 0),
        end_time=time(19, 0),
    )
    assert services.classify_time(landlord, bc_property, a_tuesday) == services.IN_HOURS

    a_tuesday_late = datetime(2026, 8, 4, 20, 0, tzinfo=tz)
    assert services.classify_time(landlord, bc_property, a_tuesday_late) == services.OUT_OF_HOURS

    a_wednesday = datetime(2026, 8, 5, 18, 0, tzinfo=tz)
    assert services.classify_time(landlord, bc_property, a_wednesday) == services.OUT_OF_HOURS


@pytest.mark.django_db
def test_property_override_replaces_default(landlord, bc_property):
    """When a property has its own windows they replace the landlord default
    for that property; other properties still inherit the default."""
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from rentium.appointments.models import AvailabilityWindow, Weekday
    from rentium.appointments import services

    tz = ZoneInfo("America/Vancouver")

    AvailabilityWindow.objects.create(
        landlord=landlord, weekday=Weekday.TUESDAY,
        start_time=time(17, 0), end_time=time(19, 0),
    )
    # Override this property to Wednesdays only.
    AvailabilityWindow.objects.create(
        landlord=landlord, property=bc_property, weekday=Weekday.WEDNESDAY,
        start_time=time(9, 0), end_time=time(11, 0),
    )

    tue = datetime(2026, 8, 4, 18, 0, tzinfo=tz)
    wed = datetime(2026, 8, 5, 10, 0, tzinfo=tz)
    # The default Tuesday no longer applies to this property...
    assert services.classify_time(landlord, bc_property, tue) == services.OUT_OF_HOURS
    # ...but its own Wednesday window does.
    assert services.classify_time(landlord, bc_property, wed) == services.IN_HOURS
    # A different property (None here) still sees the default Tuesday window.
    assert services.classify_time(landlord, None, tue) == services.IN_HOURS


# --------------------------------------------------- negotiation workflow
def _requested_appt(landlord, prop, *, lease=None, tenant_consent="NOT_APPLICABLE"):
    return Appointment.objects.create(
        landlord=landlord,
        property=prop,
        lease=lease,
        kind=Appointment.Kind.VIEWING,
        status=Appointment.Status.REQUESTED,
        starts_at=timezone.now() + timedelta(days=3),
        contact_name="Prospect Pat",
        contact_email="pat@example.com",
        tenant_consent=tenant_consent,
    )


@pytest.mark.django_db
def test_landlord_counter_then_requester_accepts(landlord, bc_property):
    appt = _requested_appt(landlord, bc_property)
    new_time = (timezone.now() + timedelta(days=4)).isoformat()

    res = _client_for(landlord).post(
        f"/api/appointments/{appt.pk}/counter/", {"starts_at": new_time}, format="json"
    )
    assert res.status_code == 200, res.data
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.AWAITING_REQUESTER
    assert appt.proposals.filter(proposed_by="LANDLORD").exists()

    anon = APIClient()
    res = anon.post(
        f"/api/public/viewing-respond/{appt.public_token}/",
        {"action": "accept"},
        format="json",
    )
    assert res.status_code == 200, res.data
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.SCHEDULED


@pytest.mark.django_db
def test_requester_counter_then_landlord_confirms(landlord, bc_property):
    appt = _requested_appt(landlord, bc_property)
    # landlord proposes...
    _client_for(landlord).post(
        f"/api/appointments/{appt.pk}/counter/",
        {"starts_at": (timezone.now() + timedelta(days=4)).isoformat()},
        format="json",
    )
    # ...requester counters back
    anon = APIClient()
    res = anon.post(
        f"/api/public/viewing-respond/{appt.public_token}/",
        {"action": "counter", "requested_time": (timezone.now() + timedelta(days=5)).isoformat()},
        format="json",
    )
    assert res.status_code == 200, res.data
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.REQUESTED
    assert appt.proposals.filter(proposed_by="REQUESTER").exists()

    # landlord confirms the requester's time
    res = _client_for(landlord).post(f"/api/appointments/{appt.pk}/confirm/")
    assert res.status_code == 200
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.SCHEDULED


@pytest.mark.django_db
def test_tenant_objection_is_advisory_landlord_can_override(
    landlord, bc_property, bc_lease, tenant_on_lease
):
    """A tenant OBJECTED never cancels the showing; the landlord can still
    confirm (they may have squared it away by phone)."""
    appt = _requested_appt(
        landlord, bc_property, lease=bc_lease, tenant_consent="PENDING"
    )
    res = _client_for(tenant_on_lease).post(
        f"/api/appointments/{appt.pk}/tenant_respond/",
        {"consent": "OBJECTED", "notes": "I work nights"},
        format="json",
    )
    assert res.status_code == 200, res.data
    appt.refresh_from_db()
    assert appt.tenant_consent == Appointment.TenantConsent.OBJECTED
    assert appt.status == Appointment.Status.REQUESTED  # NOT cancelled

    # Landlord overrides and confirms anyway.
    res = _client_for(landlord).post(f"/api/appointments/{appt.pk}/confirm/")
    assert res.status_code == 200
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.SCHEDULED


@pytest.mark.django_db
def test_tenant_sees_consent_pending_viewing(
    landlord, bc_property, bc_lease, tenant_on_lease
):
    """A consent-pending showing at the tenant's occupied unit is visible to
    them even while REQUESTED (unlike the landlord's other prospect pipeline)."""
    appt = _requested_appt(
        landlord, bc_property, lease=bc_lease, tenant_consent="PENDING"
    )
    res = _client_for(tenant_on_lease).get("/api/appointments/")
    ids = {row["id"] for row in res.data}
    assert str(appt.pk) in ids


@pytest.mark.django_db
def test_cannot_confirm_a_scheduled_viewing(landlord, bc_property):
    appt = _appt(
        landlord, bc_property, starts_at=timezone.now() + timedelta(days=2),
        status=Appointment.Status.SCHEDULED,
    )
    res = _client_for(landlord).post(f"/api/appointments/{appt.pk}/confirm/")
    assert res.status_code == 400


@pytest.mark.django_db
def test_tenant_review_event_notifies_current_tenant(
    landlord, bc_property, bc_lease, tenant_on_lease
):
    appt = _requested_appt(
        landlord, bc_property, lease=bc_lease, tenant_consent="PENDING"
    )
    event = appt.publish_event("appointment.tenant_review")
    process_domain_event(str(event.id))

    from rentium.events.models import Notification

    n = Notification.objects.filter(recipient=tenant_on_lease.user).first()
    assert n is not None
    assert "your home" in n.title.lower() or bc_property.name in n.title


@pytest.mark.django_db
def test_requester_counter_event_notifies_landlord(landlord, bc_property):
    appt = _requested_appt(landlord, bc_property)
    appt.status = Appointment.Status.REQUESTED
    appt.save(update_fields=["status"])
    event = appt.publish_event("appointment.countered", proposed_by="REQUESTER")
    process_domain_event(str(event.id))

    from rentium.events.models import Notification

    assert Notification.objects.filter(recipient=landlord.user).exists()


# --------------------------------------------------- inspection scheduling (A5)
def _inspection(landlord, lease):
    """A minimal ConditionInspection — enough to hang a walkthrough appointment
    off. Avoids seeding the full RTB-27 template the real build_inspection needs."""
    from rentium.leases.inspections import ConditionInspection, InspectionTemplate

    tmpl, _ = InspectionTemplate.objects.get_or_create(
        province="GENERIC", version=1, defaults={"name": "Test template"}
    )
    return ConditionInspection.objects.create(lease=lease, template=tmpl)


@pytest.mark.django_db
def test_landlord_proposes_inspection_tenant_accepts(
    landlord, bc_property, bc_lease, tenant_on_lease
):
    insp = _inspection(landlord, bc_lease)
    when = (timezone.now() + timedelta(days=2)).isoformat()

    res = _client_for(landlord).post(
        "/api/appointments/propose_inspection/",
        {"inspection": str(insp.pk), "starts_at": when},
        format="json",
    )
    assert res.status_code == 201, res.data
    appt = Appointment.objects.get(inspection=insp)
    assert appt.kind == Appointment.Kind.INSPECTION
    assert appt.status == Appointment.Status.AWAITING_REQUESTER

    # The tenant can SEE this pending inspection (unlike a prospect viewing).
    listed = _client_for(tenant_on_lease).get("/api/appointments/")
    assert str(appt.pk) in {r["id"] for r in listed.data}

    # Tenant accepts → SCHEDULED.
    res = _client_for(tenant_on_lease).post(
        f"/api/appointments/{appt.pk}/schedule_respond/",
        {"action": "accept"},
        format="json",
    )
    assert res.status_code == 200, res.data
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.SCHEDULED


@pytest.mark.django_db
def test_tenant_counters_inspection_then_landlord_confirms(
    landlord, bc_property, bc_lease, tenant_on_lease
):
    insp = _inspection(landlord, bc_lease)
    from rentium.appointments.services import propose_inspection_time

    appt = propose_inspection_time(
        landlord, insp, timezone.now() + timedelta(days=2)
    )
    # Tenant counters with another time → back to the landlord.
    res = _client_for(tenant_on_lease).post(
        f"/api/appointments/{appt.pk}/schedule_respond/",
        {"action": "counter", "starts_at": (timezone.now() + timedelta(days=3)).isoformat()},
        format="json",
    )
    assert res.status_code == 200, res.data
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.REQUESTED
    assert appt.proposals.filter(proposed_by="TENANT").exists()

    # Landlord confirms.
    res = _client_for(landlord).post(f"/api/appointments/{appt.pk}/confirm/")
    assert res.status_code == 200
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.SCHEDULED


@pytest.mark.django_db
def test_inspection_proposed_notifies_tenant(
    landlord, bc_property, bc_lease, tenant_on_lease
):
    insp = _inspection(landlord, bc_lease)
    from rentium.appointments.services import propose_inspection_time

    appt = propose_inspection_time(
        landlord, insp, timezone.now() + timedelta(days=2)
    )
    # process the emitted event
    from rentium.events.models import DomainEvent

    ev = DomainEvent.objects.filter(
        event_type="appointment.inspection_proposed"
    ).latest("created_at")
    process_domain_event(str(ev.id))

    from rentium.events.models import Notification

    assert Notification.objects.filter(recipient=tenant_on_lease.user).exists()
