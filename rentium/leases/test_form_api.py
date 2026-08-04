"""
The HTTP surface for form packs, including the unauthenticated signing link.

The isolation tests here are the point. A lease form is a legal document with
names, addresses and phone numbers on it, and the public signing route is the
only place in this app where an anonymous request can write to a lease. Both
sides of that boundary are pinned.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from rentium.leases import form_services as svc
# The landlord typing in a form's mandatory content, shared with the
# service tests so both exercise the same send-time guard.
from rentium.leases.test_lease_forms import _fill_required
from rentium.leases.lease_forms import FormStage
from rentium.leases.lease_forms import LeaseForm
from rentium.leases.lease_forms import LeaseFormTemplate
from rentium.leases.models import Lease
from rentium.leases.models import LeaseTenant
from rentium.users.models import LandlordProfile
from rentium.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def catalogue():
    call_command("seed_lease_forms", verbosity=0)
    return LeaseFormTemplate.objects.filter(landlord__isnull=True)


@pytest.fixture
def rtb8(catalogue):
    return catalogue.get(code="BC_RTB8")


@pytest.fixture
def api(landlord):
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    return client


@pytest.fixture
def other_api(db):
    other = LandlordProfile.objects.create(user=UserFactory())
    client = APIClient()
    client.force_authenticate(user=other.user)
    return client


@pytest.fixture
def lease_with_tenant(landlord, bc_property, tenant):
    lease = Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="1500.00",
        landlord_signed=True,
    )
    LeaseTenant.objects.create(
        lease=lease,
        tenant=tenant,
        rent_amount="1500.00",
        invited_email=tenant.user.email,
        has_signed=True,
    )
    return lease


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def test_catalogue_marks_unshipped_forms_unavailable(api, catalogue):
    response = api.get("/api/leases/form-templates/")
    assert response.status_code == 200

    by_code = {row["code"]: row for row in response.data}
    assert by_code["BC_RTB8"]["available"] is True
    assert by_code["BC_RTB1"]["available"] is False
    assert by_code["BC_RTB1"]["availability"] == "COMING_SOON"
    assert by_code["BC_RTB8"]["stage"] == FormStage.MOVE_OUT
    assert by_code["BC_RTB8"]["purpose"]


def test_the_catalogue_never_leaks_a_file_url(api, catalogue):
    """Production media is public-by-URL, so no payload may carry one."""
    body = str(api.get("/api/leases/form-templates/").data)
    assert "/media/" not in body
    assert ".pdf" not in body.replace("rtb8.pdf", "")


def test_page_images_render_at_the_pdfs_own_size(api, rtb8):
    response = api.get(f"/api/leases/form-templates/{rtb8.pk}/page/0/?dpi=72")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert "private" in response["Cache-Control"]

    import io

    from PIL import Image

    assert Image.open(io.BytesIO(response.content)).size == (612, 792)


def test_a_landlord_cannot_move_a_system_forms_fields(api, rtb8):
    response = api.put(
        f"/api/leases/form-templates/{rtb8.pk}/placements/", [], format="json"
    )
    assert response.status_code == 403
    assert rtb8.placements.count() == 25


def test_uploading_a_custom_form_derives_its_fields(api, rtb8):
    from django.core.files.uploadedfile import SimpleUploadedFile

    rtb8.file.open("rb")
    try:
        data = rtb8.file.read()
    finally:
        rtb8.file.close()

    response = api.post(
        "/api/leases/form-templates/",
        {
            "file": SimpleUploadedFile("addendum.pdf", data, "application/pdf"),
            "name": "Pet Addendum",
        },
        format="multipart",
    )
    assert response.status_code == 201
    assert response.data["placement_count"] == 25
    # Uploaded, read, and suggested — but not decided.
    assert response.data["stage"] == FormStage.UNCLASSIFIED
    assert response.data["suggestion"]["stage"] == FormStage.MOVE_OUT
    assert response.data["suggestion"]["confidence"] == "high"


def test_one_landlord_cannot_see_anothers_upload(api, other_api, rtb8):
    from django.core.files.uploadedfile import SimpleUploadedFile

    rtb8.file.open("rb")
    try:
        data = rtb8.file.read()
    finally:
        rtb8.file.close()
    created = api.post(
        "/api/leases/form-templates/",
        {"file": SimpleUploadedFile("mine.pdf", data, "application/pdf"), "name": "Mine"},
        format="multipart",
    ).data

    assert other_api.get(f"/api/leases/form-templates/{created['id']}/").status_code == 404
    assert other_api.get(f"/api/leases/form-templates/{created['id']}/page/0/").status_code == 404


# ---------------------------------------------------------------------------
# Attaching and sending
# ---------------------------------------------------------------------------


def test_attach_send_and_get_back_a_link(api, lease_with_tenant, rtb8):
    attached = api.post(
        "/api/leases/forms/",
        {"lease": str(lease_with_tenant.pk), "template": str(rtb8.pk)},
        format="json",
    )
    assert attached.status_code == 201
    assert len(attached.data["placements"]) == 25

    # RTB-8 states a date and time the tenant vacates. Sending it blank is
    # refused, so the landlord types them in first.
    blank = api.post(f"/api/leases/forms/{attached.data['id']}/send/", {}, format="json")
    assert blank.status_code == 400
    assert "can't go out blank" in str(blank.data)

    api.patch(
        f"/api/leases/forms/{attached.data['id']}/values/",
        {"values": {"date": "31/08/2026", "time": "1:00 PM"}},
        format="json",
    )

    sent = api.post(f"/api/leases/forms/{attached.data['id']}/send/", {}, format="json")
    assert sent.status_code == 200
    assert sent.data["form"]["status"] == LeaseForm.Status.SENT
    assert any("/sign/" in link for link in sent.data["links"].values())


def test_a_tenant_can_read_the_forms_on_their_own_lease(lease_with_tenant, rtb8, tenant):
    svc.attach_form(lease_with_tenant, rtb8)
    client = APIClient()
    client.force_authenticate(user=tenant.user)

    response = client.get(f"/api/leases/forms/?lease={lease_with_tenant.pk}")
    assert response.status_code == 200
    assert len(response.data) == 1


def test_a_stranger_cannot_read_them(lease_with_tenant, rtb8, other_api):
    svc.attach_form(lease_with_tenant, rtb8)
    response = other_api.get(f"/api/leases/forms/?lease={lease_with_tenant.pk}")
    assert response.status_code in (403, 404)


def test_values_can_be_typed_before_signing_and_frozen_after(
    api, lease_with_tenant, rtb8
):
    form = svc.attach_form(lease_with_tenant, rtb8)

    ok = api.patch(
        f"/api/leases/forms/{form.pk}/values/",
        {"values": {"time": "1:00 PM"}},
        format="json",
    )
    assert ok.status_code == 200
    assert ok.data["values"]["time"] == "1:00 PM"

    bad = api.patch(
        f"/api/leases/forms/{form.pk}/values/",
        {"values": {"not_a_field": "x"}},
        format="json",
    )
    assert bad.status_code == 400

    svc.send_form(_fill_required(form), notify=False)
    svc.sign_form(form.signers.first(), typed_name="Raj Singh")

    frozen = api.patch(
        f"/api/leases/forms/{form.pk}/values/",
        {"values": {"time": "9:00 AM"}},
        format="json",
    )
    assert frozen.status_code == 400


def test_activation_status_names_the_form_that_is_blocking(
    api, lease_with_tenant, rtb8
):
    """Otherwise a stuck lease looks like a bug rather than an outstanding form."""
    rtb8.stage = FormStage.WITH_LEASE
    rtb8.save(update_fields=["stage"])
    form = svc.attach_form(lease_with_tenant, rtb8, title="Pet Addendum")

    response = api.get(f"/api/leases/{lease_with_tenant.pk}/activation-status/")
    assert response.status_code == 200
    assert response.data["can_activate"] is False
    assert any("Pet Addendum" in reason for reason in response.data["blockers"])
    assert response.data["blocking_forms"][0]["id"] == str(form.pk)


# ---------------------------------------------------------------------------
# The public signing link
# ---------------------------------------------------------------------------


@pytest.fixture
def sent_form(lease_with_tenant, rtb8):
    form = svc.attach_form(lease_with_tenant, rtb8)
    svc.send_form(_fill_required(form), notify=False)
    return form


@pytest.fixture
def anon():
    return APIClient()


def test_the_link_shows_the_document_without_any_account(anon, sent_form):
    signer = sent_form.signers.first()
    response = anon.get(f"/api/public/lease-forms/{signer.sign_token}/")

    assert response.status_code == 200
    assert response.data["title"] == sent_form.title
    assert response.data["signer"]["has_signed"] is False
    assert response.data["my_fields"], "a signer must be shown their own boxes"
    # Only their own: nobody should be able to sign in someone else's slot.
    assert all(
        field["signer_role"] == signer.role for field in response.data["my_fields"]
    )


def test_opening_the_link_is_recorded_once_not_forty_times(anon, sent_form):
    from rentium.leases.lease_forms import LeaseFormEvent

    signer = sent_form.signers.first()
    for _ in range(4):
        anon.get(f"/api/public/lease-forms/{signer.sign_token}/")

    assert (
        sent_form.events.filter(kind=LeaseFormEvent.Kind.LINK_OPENED).count() == 1
    )


def test_signing_through_the_link_records_full_evidence(anon, sent_form):
    signer = sent_form.signers.get(role="TENANT", order=0)
    response = anon.post(
        f"/api/public/lease-forms/{signer.sign_token}/sign/",
        {"typed_name": "Sarah Chen", "method": "TYPED"},
        format="json",
        HTTP_USER_AGENT="Mozilla/5.0 (iPhone)",
        REMOTE_ADDR="203.0.113.7",
    )
    assert response.status_code == 200
    assert response.data["signer"]["has_signed"] is True

    signature = sent_form.signatures.get(signer=signer)
    assert signature.typed_name == "Sarah Chen"
    assert signature.ip_address == "203.0.113.7"
    assert "iPhone" in signature.user_agent
    assert signature.template_sha256 == sent_form.template.sha256


def test_a_used_link_stops_working(anon, sent_form):
    signer = sent_form.signers.first()
    body = {"typed_name": "Sarah Chen"}
    url = f"/api/public/lease-forms/{signer.sign_token}/sign/"

    assert anon.post(url, body, format="json").status_code == 200
    assert anon.post(url, body, format="json").status_code == 403


def test_an_expired_link_stops_working(anon, sent_form):
    from datetime import timedelta

    from django.utils import timezone

    signer = sent_form.signers.first()
    signer.token_expires_at = timezone.now() - timedelta(days=1)
    signer.save(update_fields=["token_expires_at"])

    response = anon.post(
        f"/api/public/lease-forms/{signer.sign_token}/sign/",
        {"typed_name": "Sarah Chen"},
        format="json",
    )
    assert response.status_code == 403


def test_a_made_up_token_is_a_flat_404(anon):
    import uuid

    assert anon.get(f"/api/public/lease-forms/{uuid.uuid4()}/").status_code == 404


def test_an_empty_name_is_not_a_signature(anon, sent_form):
    signer = sent_form.signers.first()
    response = anon.post(
        f"/api/public/lease-forms/{signer.sign_token}/sign/",
        {"typed_name": "  "},
        format="json",
    )
    assert response.status_code == 400
    assert not sent_form.signatures.exists()


def test_the_public_payload_carries_no_token_for_anyone_else(anon, sent_form):
    """Holding one link must not reveal the others."""
    signer = sent_form.signers.first()
    body = str(anon.get(f"/api/public/lease-forms/{signer.sign_token}/").data)

    for other in sent_form.signers.exclude(pk=signer.pk):
        assert str(other.sign_token) not in body


def test_the_signer_can_read_the_whole_document_first(anon, sent_form):
    signer = sent_form.signers.first()
    response = anon.get(f"/api/public/lease-forms/{signer.sign_token}/pdf/")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content[:5] == b"%PDF-"


def test_declining_is_recorded_and_ends_the_link(anon, sent_form):
    signer = sent_form.signers.first()
    response = anon.post(
        f"/api/public/lease-forms/{signer.sign_token}/decline/",
        {"reason": "The date doesn't work"},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["signer"]["declined"] is True

    blocked = anon.post(
        f"/api/public/lease-forms/{signer.sign_token}/sign/",
        {"typed_name": "Sarah Chen"},
        format="json",
    )
    assert blocked.status_code == 403


def test_everyone_signing_executes_the_document(anon, sent_form):
    for signer in sent_form.signers.all():
        response = anon.post(
            f"/api/public/lease-forms/{signer.sign_token}/sign/",
            {"typed_name": f"Signer {signer.order}"},
            format="json",
        )
        assert response.status_code == 200

    sent_form.refresh_from_db()
    assert sent_form.status == LeaseForm.Status.COMPLETED
    assert sent_form.executed_sha256


# ---------------------------------------------------------------------------
# The landlord's own two jobs: correct the details, and sign
# ---------------------------------------------------------------------------


def test_the_landlord_is_told_they_have_a_signature_outstanding(
    api, lease_with_tenant, rtb8
):
    """Without this the dashboard cannot offer a Sign button at all, and the
    person who created the document has to go find their own email."""
    form = svc.attach_form(lease_with_tenant, rtb8)
    svc.send_form(_fill_required(form), notify=False)

    row = api.get(f"/api/leases/forms/{form.pk}/").data
    assert row["my_signature"]["role"] == "LANDLORD"
    assert row["my_signature"]["can_sign"] is True
    assert row["my_signature"]["has_signed"] is False


def test_the_landlord_can_sign_in_the_dashboard(api, lease_with_tenant, rtb8):
    form = svc.attach_form(lease_with_tenant, rtb8)
    svc.send_form(_fill_required(form), notify=False)

    response = api.post(
        f"/api/leases/forms/{form.pk}/sign/",
        {"typed_name": "Raj Singh", "method": "TYPED"},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["my_signature"]["has_signed"] is True
    assert response.data["my_signature"]["can_sign"] is False

    # Same evidence as the public link — not a weaker in-app shortcut.
    signature = form.signatures.get()
    assert signature.typed_name == "Raj Singh"
    assert signature.template_sha256 == rtb8.sha256
    assert signature.values_sha256


def test_a_tenant_is_never_offered_the_landlords_slot(
    lease_with_tenant, rtb8, tenant
):
    form = svc.attach_form(lease_with_tenant, rtb8)
    svc.send_form(_fill_required(form), notify=False)

    client = APIClient()
    client.force_authenticate(user=tenant.user)
    row = client.get(f"/api/leases/forms/{form.pk}/").data

    assert row["my_signature"]["role"] == "TENANT"


def test_a_landlord_can_correct_a_prefilled_name_and_add_their_address(
    api, lease_with_tenant, rtb8
):
    """The two things they said they had no way to do."""
    form = svc.attach_form(lease_with_tenant, rtb8)

    response = api.patch(
        f"/api/leases/forms/{form.pk}/values/",
        {
            "values": {
                "last_name_s": "de la Cruz",
                "street__and_name": "12 Fort St",
                "city": "Victoria",
                "postal_code": "V8W 1H8",
            }
        },
        format="json",
    )
    assert response.status_code == 200
    assert response.data["values"]["last_name_s"] == "de la Cruz"
    assert response.data["values"]["street__and_name"] == "12 Fort St"
    # The tenant's own block is untouched by any of it.
    assert response.data["values"]["street__and_name_2"] == "1234 Oak Ave"


def test_details_stay_editable_after_sending_until_someone_signs(
    api, lease_with_tenant, rtb8
):
    form = svc.attach_form(lease_with_tenant, rtb8)
    svc.send_form(_fill_required(form), notify=False)

    sent = api.patch(
        f"/api/leases/forms/{form.pk}/values/",
        {"values": {"time": "9:00 AM"}},
        format="json",
    )
    assert sent.status_code == 200, "a typo must be fixable before anyone signs"

    svc.sign_form(form.signers.first(), typed_name="Raj Singh")
    frozen = api.patch(
        f"/api/leases/forms/{form.pk}/values/",
        {"values": {"time": "10:00 AM"}},
        format="json",
    )
    assert frozen.status_code == 400
