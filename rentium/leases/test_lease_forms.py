"""
Lease form packs, end to end through the service layer.

The tests that matter most here are the lifecycle ones: whether an unsigned
form holds a lease back, whether it can drag a live tenancy backwards (it must
not), and whether a signed RTB-8 actually ends a tenancy. Those are the
behaviours that touch rent and legal dates, and none of them are visible from
the model definitions alone.
"""

from __future__ import annotations

from datetime import date
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError

from rentium.leases import form_services as svc
from rentium.leases.lease_forms import FormStage
from rentium.leases.lease_forms import LeaseForm
from rentium.leases.lease_forms import LeaseFormEvent
from rentium.leases.lease_forms import LeaseFormSignature
from rentium.leases.lease_forms import LeaseFormTemplate
from rentium.leases.lease_forms import SignerRole
from rentium.leases.models import Lease
from rentium.leases.models import LeaseTenant

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def catalogue():
    """The real seeded catalogue — same rows production runs against."""
    from django.core.management import call_command

    call_command("seed_lease_forms", verbosity=0)
    return LeaseFormTemplate.objects.filter(landlord__isnull=True)


@pytest.fixture
def rtb8(catalogue) -> LeaseFormTemplate:
    return catalogue.get(code="BC_RTB8")


@pytest.fixture
def addendum(rtb8, landlord) -> LeaseFormTemplate:
    """A WITH_LEASE form, reusing RTB-8's file so the geometry is real."""
    rtb8.file.open("rb")
    try:
        data = rtb8.file.read()
    finally:
        rtb8.file.close()

    from django.core.files.uploadedfile import SimpleUploadedFile

    template, _created = svc.upload_template(
        landlord,
        SimpleUploadedFile("pet-addendum.pdf", data, content_type="application/pdf"),
        name="Pet Addendum",
        stage=FormStage.WITH_LEASE,
    )
    return template


@pytest.fixture
def pending_lease(landlord, bc_property, tenant) -> Lease:
    """A lease waiting on signatures, with the landlord and one tenant signed.

    Everything except an attached form is in place, so a test that adds one is
    testing the form and nothing else.
    """
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


def _sign_everyone(form: LeaseForm, name: str = "A Signer"):
    for signer in form.signers.all():
        if not signer.has_signed:
            svc.sign_form(signer, typed_name=f"{name} {signer.role}")


def _fill_required(form: LeaseForm):
    """Type in whatever content the form insists on before it can go out.

    Stands in for the landlord filling the boxes in the UI. RTB-8 needs a vacate
    date and time; send_form refuses without them, which is the behaviour
    test_a_form_cannot_be_sent_with_its_key_facts_blank pins.

    Keyed on `key`, never on `label` — RTB-8 prints the same label over its
    landlord and tenant blocks, so matching by label fills one twice and leaves
    the other blank.
    """
    for row in svc.unfilled_required_rows(form):
        form.values[row["key"]] = (
            "31/08/2026" if row["kind"] == "DATE" else f"({row['key']})"
        )
    form.save(update_fields=["values", "updated_at"])
    return form


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def test_seed_is_idempotent(catalogue):
    from django.core.management import call_command

    before = list(catalogue.values_list("pk", "sha256"))
    call_command("seed_lease_forms", verbosity=0)
    assert list(catalogue.values_list("pk", "sha256")) == before


def test_rtb8_ships_available_with_its_fields_placed(rtb8):
    assert rtb8.is_selectable
    assert rtb8.stage == FormStage.MOVE_OUT
    assert rtb8.binds_to == "moveout"
    assert rtb8.placements.count() == 25
    assert rtb8.placements.filter(kind="SIGNATURE").count() == 3


def test_unshipped_forms_are_catalogued_but_not_selectable(catalogue):
    """A landlord should see that we know a form exists before we ship it."""
    coming = catalogue.filter(availability=LeaseFormTemplate.Availability.COMING_SOON)
    assert coming.exists()
    assert not any(row.is_selectable for row in coming)


def test_catalogue_shows_system_forms_and_only_your_own_uploads(
    landlord, tenant, addendum
):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    other = LandlordProfile.objects.create(user=UserFactory())
    mine = {t.pk for t in svc.catalog_for(landlord)}
    theirs = {t.pk for t in svc.catalog_for(other)}

    assert addendum.pk in mine
    assert addendum.pk not in theirs


def test_reuploading_the_same_bytes_returns_the_same_template(landlord, addendum):
    from django.core.files.uploadedfile import SimpleUploadedFile

    addendum.file.open("rb")
    try:
        data = addendum.file.read()
    finally:
        addendum.file.close()

    template, created = svc.upload_template(
        landlord,
        SimpleUploadedFile("again.pdf", data, content_type="application/pdf"),
        name="Pet Addendum (again)",
    )
    assert created is False
    assert template.pk == addendum.pk


def test_a_non_pdf_is_refused_by_its_bytes_not_its_name(landlord):
    from django.core.files.uploadedfile import SimpleUploadedFile

    with pytest.raises(ValidationError):
        svc.upload_template(
            landlord,
            SimpleUploadedFile("lease.pdf", b"GIF89a not really", content_type="application/pdf"),
            name="Fake",
        )


def test_an_upload_gets_a_suggestion_but_never_a_stage(landlord, rtb8):
    """OCR proposes; a human disposes. Nothing auto-classifies itself."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    rtb8.file.open("rb")
    try:
        data = rtb8.file.read()
    finally:
        rtb8.file.close()

    template, _ = svc.upload_template(
        landlord,
        SimpleUploadedFile("mystery.pdf", data, content_type="application/pdf"),
        name="Mystery",
    )
    assert template.stage == FormStage.UNCLASSIFIED
    assert template.suggested_stage == FormStage.MOVE_OUT
    assert template.suggestion_signals["confidence"] in {"medium", "high"}


# ---------------------------------------------------------------------------
# Attaching
# ---------------------------------------------------------------------------


def test_attaching_freezes_the_placements(bc_lease, rtb8):
    form = svc.attach_form(bc_lease, rtb8, title="RTB-8")
    assert len(form.placements_snapshot) == 25

    rtb8.placements.all().delete()
    form.refresh_from_db()
    assert len(form.placements_snapshot) == 25, (
        "editing a template must never reach into an attached form"
    )


def test_attaching_prefills_what_the_lease_already_knows(bc_lease, rtb8):
    form = svc.attach_form(bc_lease, rtb8)
    # The TENANT's address block — where the rental address belongs. The
    # landlord block is their address for service, which Rentium does not hold
    # and therefore does not invent.
    assert form.values["street__and_name_2"] == "1234 Oak Ave"
    assert form.values["city_2"] == "Victoria"
    assert form.values["province_2"] == "BC"


def test_an_unclassified_form_cannot_be_attached(bc_lease, landlord, rtb8):
    from django.core.files.uploadedfile import SimpleUploadedFile

    rtb8.file.open("rb")
    try:
        data = rtb8.file.read()
    finally:
        rtb8.file.close()
    template, _ = svc.upload_template(
        landlord,
        SimpleUploadedFile("mystery.pdf", data, content_type="application/pdf"),
        name="Mystery",
    )
    with pytest.raises(ValidationError):
        svc.attach_form(bc_lease, template)


def test_a_coming_soon_form_cannot_be_attached(bc_lease, catalogue):
    with pytest.raises(ValidationError):
        svc.attach_form(bc_lease, catalogue.get(code="BC_RTB1"))


def test_you_cannot_attach_another_landlords_upload(bc_lease, addendum):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    addendum.landlord = LandlordProfile.objects.create(user=UserFactory())
    addendum.save(update_fields=["landlord"])
    with pytest.raises(ValidationError):
        svc.attach_form(bc_lease, addendum)


def test_an_unknown_prefill_source_is_an_error_not_a_blank(bc_lease, rtb8):
    placement = rtb8.placements.first()
    placement.auto_source = "tenant.favourite_colour"
    placement.save(update_fields=["auto_source"])
    with pytest.raises(ValidationError):
        svc.attach_form(bc_lease, rtb8)


# ---------------------------------------------------------------------------
# Activation gating — the behaviour the landlord actually feels
# ---------------------------------------------------------------------------


def test_an_unsigned_with_lease_form_holds_the_lease_at_pending(
    pending_lease, addendum
):
    form = svc.attach_form(pending_lease, addendum)
    assert form.blocks_activation is True

    assert pending_lease.check_and_activate() is False
    pending_lease.refresh_from_db()
    assert pending_lease.status == Lease.LeaseStatus.PENDING_SIGNATURES

    reasons = svc.activation_blockers(pending_lease)
    assert any("Pet Addendum" in str(reason) for reason in reasons)


def test_signing_the_form_activates_the_lease(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    _sign_everyone(form)

    form.refresh_from_db()
    pending_lease.refresh_from_db()
    assert form.status == LeaseForm.Status.COMPLETED
    assert pending_lease.status == Lease.LeaseStatus.ACTIVE


def test_voiding_the_last_blocking_form_releases_the_lease(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    assert pending_lease.check_and_activate() is False

    svc.void_form(form, reason="Not needed after all")

    pending_lease.refresh_from_db()
    assert pending_lease.status == Lease.LeaseStatus.ACTIVE


def test_a_form_attached_to_a_live_lease_never_pushes_it_backwards(
    bc_lease, addendum
):
    """The rule most likely to be got wrong later, so it is pinned here.

    Rent is already being charged against an ACTIVE lease and occupancy is
    already open. Late paperwork is an obligation, not a reason to un-start a
    tenancy.
    """
    assert bc_lease.status == Lease.LeaseStatus.ACTIVE

    form = svc.attach_form(bc_lease, addendum)

    assert form.blocks_activation is False
    assert not svc.blocking_forms(bc_lease).exists()
    bc_lease.refresh_from_db()
    assert bc_lease.status == Lease.LeaseStatus.ACTIVE
    # It is still tracked as outstanding — just not as a blocker.
    assert svc.outstanding_forms(bc_lease).count() == 1


def test_an_optional_form_never_blocks(pending_lease, addendum):
    svc.attach_form(pending_lease, addendum, required=False)
    assert not svc.blocking_forms(pending_lease).exists()


# ---------------------------------------------------------------------------
# Sending and signing
# ---------------------------------------------------------------------------


def test_send_binds_roster_people_to_the_placeholder_slots(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)

    tenant_signer = form.signers.get(role=SignerRole.TENANT, order=0)
    assert tenant_signer.lease_tenant_id is not None
    assert tenant_signer.email == pending_lease.lease_tenants.first().invited_email
    assert tenant_signer.sign_token is not None
    assert tenant_signer.token_expires_at is not None


def test_send_accepts_a_typed_signer_when_the_lease_has_no_invitee(
    landlord, bc_property, rtb8
):
    """The 'boxes placed before anyone is invited' case, start to finish."""
    lease = Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.DRAFT,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="900.00",
    )
    form = svc.attach_form(lease, rtb8)

    with pytest.raises(ValidationError):
        svc.send_form(_fill_required(form), notify=False)  # nobody to send it to yet

    svc.send_form(
        form,
        notify=False,
        manual_signers={"TENANT:0": {"name": "Sarah Chen", "email": "sarah@x.test"}},
    )
    signer = form.signers.get(role=SignerRole.TENANT, order=0)
    assert signer.display_name == "Sarah Chen"
    assert signer.email == "sarah@x.test"


def test_signing_records_who_when_and_against_which_bytes(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    signer = form.signers.get(role=SignerRole.TENANT, order=0)

    signature = svc.sign_form(
        signer,
        typed_name="Sarah Chen",
        ip_address="203.0.113.9",
        user_agent="Mozilla/5.0",
    )
    assert signature.typed_name == "Sarah Chen"
    assert signature.ip_address == "203.0.113.9"
    assert signature.template_sha256 == addendum.sha256
    assert signature.values_sha256

    signer.refresh_from_db()
    assert signer.has_signed


def test_nobody_signs_twice(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    signer = form.signers.first()
    svc.sign_form(signer, typed_name="Sarah Chen")
    signer.refresh_from_db()
    with pytest.raises(ValidationError):
        svc.sign_form(signer, typed_name="Sarah Chen")


def test_a_blank_name_is_not_a_signature(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    with pytest.raises(ValidationError):
        svc.sign_form(form.signers.first(), typed_name="   ")


def test_a_voided_form_cannot_be_signed(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    signer = form.signers.first()
    svc.void_form(form, reason="withdrawn")
    signer.refresh_from_db()
    with pytest.raises(ValidationError):
        svc.sign_form(signer, typed_name="Sarah Chen")


def test_a_drawn_signature_needs_an_image(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    with pytest.raises(ValidationError):
        svc.sign_form(
            form.signers.first(),
            typed_name="Sarah Chen",
            method=LeaseFormSignature.Method.DRAWN,
        )


def test_a_signature_image_must_actually_be_a_png():
    with pytest.raises(ValidationError):
        svc.decode_signature_png("data:image/png;base64,R0lGODlhAQABAAAAACw=")


def test_reminders_go_only_to_people_who_still_owe_a_signature(
    pending_lease, addendum, mailoutbox
):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    signers = list(form.signers.all())
    svc.sign_form(signers[0], typed_name="First Signer")

    mailoutbox.clear()
    sent = svc.remind_outstanding(form)

    assert sent == len(signers) - 1
    assert len(mailoutbox) == sent


def test_the_reminder_email_says_the_lease_is_waiting_on_it(
    pending_lease, addendum, mailoutbox
):
    """A tenant who already signed the lease has no other way to know."""
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(form)

    assert mailoutbox
    body = mailoutbox[-1].body
    assert "Pet Addendum" in body
    assert "doesn't take effect" in body or "does not take effect" in body


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def test_completion_stamps_hashes_and_freezes_the_document(pending_lease, addendum):
    import hashlib

    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    _sign_everyone(form)
    form.refresh_from_db()

    assert form.status == LeaseForm.Status.COMPLETED
    assert form.completed_at is not None
    assert form.executed_file

    form.executed_file.open("rb")
    try:
        data = form.executed_file.read()
    finally:
        form.executed_file.close()
    assert hashlib.sha256(data).hexdigest() == form.executed_sha256

    # The stored bytes are what people signed; render must not re-derive them.
    assert svc.render_form_pdf(form) == data


def test_an_executed_document_cannot_be_swapped(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    _sign_everyone(form)
    form.refresh_from_db()

    form.executed_sha256 = "0" * 64
    with pytest.raises(ValidationError):
        form.save(update_fields=["executed_sha256"])


def test_a_completed_form_cannot_be_voided(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    _sign_everyone(form)
    form.refresh_from_db()
    with pytest.raises(ValidationError):
        svc.void_form(form)


def test_a_form_cannot_be_sent_with_its_key_facts_blank(pending_lease, rtb8):
    """A mutual agreement to end a tenancy with no end date on it is not a
    document anyone should be asked to sign."""
    form = svc.attach_form(pending_lease, rtb8)
    missing = svc.unfilled_required_fields(form)
    # The vacate date and time — the whole point of the form, and the only
    # things Rentium cannot know. Both parties' names come off the lease.
    assert missing == ["time", "Date"]

    with pytest.raises(ValidationError):
        svc.send_form(form, notify=False)

    svc.send_form(_fill_required(form), notify=False)
    assert form.signers.exists()


def test_signing_never_invents_a_name_for_an_empty_name_box(pending_lease, rtb8):
    """RTB-8 splits a party across "first and middle" and "Last name(s)".

    A signature is one string. Spreading it across both boxes printed "Raj
    Singh" in the surname field of a government form — a wrong fact, produced by
    the system rather than by anyone.
    """
    form = svc.attach_form(pending_lease, rtb8)
    svc.send_form(_fill_required(form), notify=False)

    # Blanked AFTER sending, so prefill (a legitimate, separate mechanism) can't
    # refill them and the only thing that could is the signature itself.
    form.values["last_name_s"] = ""
    form.values["first_and_middle_names"] = ""
    form.save(update_fields=["values"])

    landlord_signer = form.signers.get(role=SignerRole.LANDLORD, order=0)
    svc.sign_form(landlord_signer, typed_name="Raj Singh")

    values = svc.rendered_values(form)
    assert values["signature6"] == "Raj Singh"
    assert not values.get("last_name_s")
    assert not values.get("first_and_middle_names")


def test_only_the_date_signed_box_is_filled_by_signing(pending_lease, rtb8):
    """The vacate date and the date signed are both DATE boxes and mean
    completely different things."""
    form = svc.attach_form(pending_lease, rtb8)
    _fill_required(form)
    form.values["date"] = "31/12/2026"  # the agreed vacate date
    form.save(update_fields=["values"])
    svc.send_form(form, notify=False)
    svc.sign_form(form.signers.first(), typed_name="Raj Singh")

    values = svc.rendered_values(form)
    assert values["date"] == "31/12/2026", "the vacate date must not drift to today"
    assert values["text2"] == date.today().strftime("%d/%m/%Y")


def test_a_split_name_is_a_proposal_the_landlord_can_correct(bc_lease, rtb8):
    form = svc.attach_form(bc_lease, rtb8)
    # Naive split, applied where a form demands two boxes — and editable.
    assert "last_name_s" in form.values
    form.values["last_name_s"] = "de la Cruz"
    form.save(update_fields=["values"])
    assert svc.rendered_values(form)["last_name_s"] == "de la Cruz"


def test_the_signature_lands_on_the_document(pending_lease, addendum):
    import io

    from pypdf import PdfReader

    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    for signer in form.signers.all():
        svc.sign_form(signer, typed_name="Sarah Chen")
    form.refresh_from_db()

    text = PdfReader(io.BytesIO(svc.render_form_pdf(form))).pages[0].extract_text()
    assert "Sarah Chen" in text


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_signatures_and_events_are_append_only(pending_lease, addendum):
    form = svc.attach_form(pending_lease, addendum)
    svc.send_form(_fill_required(form), notify=False)
    signature = svc.sign_form(form.signers.first(), typed_name="Sarah Chen")

    signature.typed_name = "Someone Else"
    with pytest.raises(ValidationError):
        signature.save()
    with pytest.raises(ValidationError):
        signature.delete()

    event = form.events.first()
    with pytest.raises(ValidationError):
        event.save()
    with pytest.raises(ValidationError):
        event.delete()


def test_link_opened_is_debounced(pending_lease, addendum):
    """A signing page re-fetches on mount; 40 'opened' rows are noise."""
    form = svc.attach_form(pending_lease, addendum)
    for _ in range(5):
        svc.record_form_event(
            form, LeaseFormEvent.Kind.LINK_OPENED, debounce_seconds=120
        )
    assert form.events.filter(kind=LeaseFormEvent.Kind.LINK_OPENED).count() == 1


# ---------------------------------------------------------------------------
# Move-out: the RTB-8 workflow
# ---------------------------------------------------------------------------


@pytest.fixture
def moveout(bc_lease, tenant):
    from rentium.leases.moveout import MoveOutRequest

    lease_tenant = LeaseTenant.objects.create(
        lease=bc_lease,
        tenant=tenant,
        rent_amount="850.00",
        invited_email=tenant.user.email,
        has_signed=True,
    )
    return MoveOutRequest.objects.create(
        lease=bc_lease,
        lease_tenant=lease_tenant,
        initiated_by=MoveOutRequest.InitiatedBy.LANDLORD,
        kind=MoveOutRequest.Kind.MUTUAL_AGREEMENT,
        requested_end_date=date.today() + timedelta(days=20),
        form_type="RTB-8",
    )


def test_a_mutual_agreement_gets_the_provinces_form(moveout, catalogue):
    form = svc.ensure_mutual_agreement_form(moveout)
    assert form is not None
    assert form.template.code == "BC_RTB8"
    assert form.moveout_request_id == moveout.pk
    # The vacate date the request asked for is already on the paper.
    assert form.values["date"] == moveout.requested_end_date.strftime("%d/%m/%Y")


def test_the_form_is_attached_only_once(moveout, catalogue):
    first = svc.ensure_mutual_agreement_form(moveout)
    assert svc.ensure_mutual_agreement_form(moveout).pk == first.pk


def test_an_ended_by_agreement_form_is_not_a_lease_blocker(moveout, catalogue):
    form = svc.ensure_mutual_agreement_form(moveout)
    assert form.blocks_activation is False


def test_signing_the_rtb8_ends_the_tenancy(moveout, catalogue):
    from rentium.leases.moveout import MoveOutRequest

    form = svc.ensure_mutual_agreement_form(moveout)
    svc.send_form(_fill_required(form), notify=False)
    _sign_everyone(form)

    moveout.refresh_from_db()
    form.refresh_from_db()
    assert form.status == LeaseForm.Status.COMPLETED
    assert moveout.tenant_signed and moveout.landlord_signed
    assert moveout.status == MoveOutRequest.Status.ACCEPTED
    assert moveout.effective_end_date == moveout.requested_end_date

    moveout.lease.refresh_from_db()
    assert moveout.lease.move_out_date == moveout.requested_end_date


def test_the_date_follows_the_request_until_someone_signs(moveout, catalogue):
    form = svc.ensure_mutual_agreement_form(moveout)
    moved = date.today() + timedelta(days=45)

    svc.sync_moveout_date(form, moved)

    moveout.refresh_from_db()
    form.refresh_from_db()
    assert moveout.requested_end_date == moved
    assert form.values["date"] == moved.strftime("%d/%m/%Y")


def test_the_date_freezes_the_moment_the_paper_is_signed(moveout, catalogue):
    """After a signature the document is the authority, not the request."""
    form = svc.ensure_mutual_agreement_form(moveout)
    svc.send_form(_fill_required(form), notify=False)
    svc.sign_form(form.signers.first(), typed_name="Raj Singh")

    with pytest.raises(ValidationError):
        svc.sync_moveout_date(form, date.today() + timedelta(days=60))


# ---------------------------------------------------------------------------
# Attention
# ---------------------------------------------------------------------------


def test_a_blocking_form_is_urgent_and_an_outstanding_one_is_not(
    landlord, pending_lease, bc_lease, addendum
):
    from rentium.attention.service import compute_attention

    blocking = svc.attach_form(pending_lease, addendum)
    svc.send_form(blocking, notify=False)
    svc.attach_form(bc_lease, addendum)

    items = {i.key: i for i in compute_attention(landlord)}
    blocking_item = items[f"lease.form:{blocking.pk}"]

    assert blocking_item.severity == "urgent"
    assert "holding up" in blocking_item.title
    assert [i for i in items.values() if i.severity == "info" and "form:" in i.key]


# ---------------------------------------------------------------------------
# August 2026: the landlord's details printed in the tenant's block
# ---------------------------------------------------------------------------


def test_the_tenant_block_fills_from_the_lease_the_moment_a_form_is_attached(
    pending_lease, rtb8, tenant
):
    """It used to stay blank until send_form created signer rows.

    So the landlord was asked to type the tenant's name — into a box labelled
    identically to their own — and their name went into the tenant's block on a
    government form.
    """
    form = svc.attach_form(pending_lease, rtb8)

    assert form.values["tenant_first_and_middle"] == _given(tenant.user.name)
    assert form.values["last_name_s_2"] == _surname_of(tenant.user.name)
    # And it is no longer something the landlord is asked for.
    assert not any(
        "name" in field.casefold() for field in svc.unfilled_required_fields(form)
    )


def test_the_two_party_blocks_never_share_a_value(pending_lease, rtb8, landlord):
    """The symptom the landlord reported: both blocks reading the same."""
    form = svc.attach_form(pending_lease, rtb8)

    assert form.values["first_and_middle_names"] == _given(landlord.user.name)
    assert form.values["tenant_first_and_middle"] != form.values[
        "first_and_middle_names"
    ]
    assert form.values["last_name_s_2"] != form.values["last_name_s"]


def test_the_rental_address_is_never_claimed_as_the_landlords_own(pending_lease, rtb8):
    """RTB-8's landlord block is an address for service, not the rental.

    Rentium holds no landlord mailing address, so the honest output is a blank
    box the landlord fills — not the property asserted as theirs.
    """
    form = svc.attach_form(pending_lease, rtb8)

    assert form.values["street__and_name_2"] == "1234 Oak Ave"  # tenant lives there
    assert not form.values.get("street__and_name")
    assert not form.values.get("city")
    assert not form.values.get("postal_code")


def test_a_province_prints_in_the_case_a_form_expects(pending_lease, rtb8):
    form = svc.attach_form(pending_lease, rtb8)
    assert form.values["province_2"] == "BC"


def test_a_second_tenant_gets_their_own_block_not_the_firsts(
    pending_lease, rtb8, landlord
):
    """signer_index is what keeps two tenants apart before anyone is invited."""
    from rentium.users.models import TenantProfile
    from rentium.users.tests.factories import UserFactory

    second = TenantProfile.objects.create(user=UserFactory(name="Wei Zhang"))
    LeaseTenant.objects.create(
        lease=pending_lease,
        tenant=second,
        rent_amount="0.00",
        invited_email=second.user.email,
    )
    form = svc.attach_form(pending_lease, rtb8)

    first_row = next(
        r for r in form.placements_snapshot if r["key"] == "tenant_first_and_middle"
    )
    assert first_row["signer_index"] == 0
    # TENANT/0 is still the first tenant, not whoever was added last.
    assert form.values["tenant_first_and_middle"] == _given(
        pending_lease.lease_tenants.order_by("created_at").first().display_name
    )

    roster = svc.roster_candidates(pending_lease)
    assert roster[(SignerRole.TENANT, 1)]["name"] == "Wei Zhang"


def _given(full_name):
    parts = (full_name or "").split()
    return " ".join(parts[:-1]) if len(parts) > 1 else ""


def _surname_of(full_name):
    parts = (full_name or "").split()
    return parts[-1] if parts else ""


def test_a_repeated_label_still_says_which_block_it_means(landlord, bc_property, rtb8):
    """The safeguard for a lease with nobody on it yet.

    Both parties' names normally come off the lease. When there is no tenant to
    read, the landlord is asked — and RTB-8 prints "Last name (s)" over both
    blocks, so an unqualified prompt is how their own name reached the tenant's.
    """
    lease = Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.DRAFT,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="900.00",
    )
    missing = svc.unfilled_required_fields(svc.attach_form(lease, rtb8))

    assert "Tenant's Last name (s)" in missing
    assert "Last name (s)" not in missing
    # A label that appears once is left alone — "Landlord's time" would be a
    # worse name for the hour the tenant vacates than "time" is.
    assert "time" in missing


# ---------------------------------------------------------------------------
# Sign first, then send — the order a landlord actually works in
# ---------------------------------------------------------------------------


def test_the_landlord_can_sign_before_the_form_is_sent(pending_lease, rtb8):
    """Signer rows used to appear only inside send_form.

    So the order was: email the tenant a link to a document with an empty
    landlord signature, and only then get the ability to sign it yourself.
    """
    form = svc.attach_form(pending_lease, rtb8)

    assert form.status == LeaseForm.Status.DRAFT
    me = svc.signer_for_user(form, pending_lease.landlord.user)
    assert me is not None, "the landlord must be able to sign their own form"

    svc.sign_form(me, typed_name="Raj Singh")
    assert svc.rendered_values(form)["signature6"] == "Raj Singh"


def test_slots_the_lease_cannot_fill_stay_unbound(landlord, bc_property, rtb8):
    """A tenant nobody has invited yet is still nobody."""
    lease = Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.DRAFT,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="900.00",
    )
    form = svc.attach_form(lease, rtb8)

    assert form.signers.filter(role=SignerRole.LANDLORD).exists()
    assert not form.signers.filter(role=SignerRole.TENANT).exists()


def test_a_token_that_was_never_sent_does_not_work(pending_lease, rtb8):
    """Rows now exist before anything is emailed, so an unissued credential
    must not authenticate."""
    form = svc.attach_form(pending_lease, rtb8)
    signer = form.signers.get(role=SignerRole.TENANT, order=0)

    assert signer.sent_at is None
    assert signer.token_is_live is False

    svc.send_form(_fill_required(form), notify=False)
    signer.refresh_from_db()
    assert signer.sent_at is not None
    assert signer.token_is_live is True


def test_signing_first_then_sending_leaves_only_the_tenant_outstanding(
    pending_lease, rtb8
):
    form = svc.attach_form(pending_lease, rtb8)
    svc.sign_form(
        svc.signer_for_user(form, pending_lease.landlord.user), typed_name="Raj Singh"
    )
    svc.send_form(_fill_required(form), notify=False)
    form.refresh_from_db()

    outstanding = [s.display_name for s in form.signers.filter(signed_at__isnull=True)]
    assert len(outstanding) == 1
    assert form.status == LeaseForm.Status.PARTIALLY_SIGNED
    # The landlord's earlier signature survives being sent.
    assert form.signatures.get().typed_name == "Raj Singh"


def test_a_form_with_no_signers_can_be_repaired(pending_lease, rtb8):
    """What migration 0026 does to forms attached before signers were bound.

    Their symptom was a downloaded PDF with an empty signature block and no way
    to change that: the Sign button is drawn from signer rows, so with none
    there was nothing to press.
    """
    form = svc.attach_form(pending_lease, rtb8)
    form.signers.all().delete()  # the state those forms were left in
    assert svc.signer_for_user(form, pending_lease.landlord.user) is None

    svc.bind_known_signers(form)

    assert svc.signer_for_user(form, pending_lease.landlord.user) is not None
    assert form.signers.count() == 2


def test_binding_signers_twice_changes_nothing(pending_lease, rtb8):
    form = svc.attach_form(pending_lease, rtb8)
    before = sorted(str(s.pk) for s in form.signers.all())

    svc.bind_known_signers(form)

    assert sorted(str(s.pk) for s in form.signers.all()) == before


def test_repairing_never_disturbs_a_signature_already_given(pending_lease, rtb8):
    form = svc.attach_form(pending_lease, rtb8)
    me = svc.signer_for_user(form, pending_lease.landlord.user)
    svc.sign_form(me, typed_name="Raj Singh")

    svc.bind_known_signers(form)

    me.refresh_from_db()
    assert me.signed_at is not None
    assert svc.rendered_values(form)["signature6"] == "Raj Singh"


def test_a_signature_reaches_the_downloadable_pdf(pending_lease, rtb8):
    """The whole point, asserted on the bytes a landlord actually receives."""
    import io

    from pypdf import PdfReader

    form = svc.attach_form(pending_lease, rtb8)
    svc.sign_form(
        svc.signer_for_user(form, pending_lease.landlord.user), typed_name="Raj Singh"
    )

    text = PdfReader(io.BytesIO(svc.render_form_pdf(form))).pages[0].extract_text()
    assert "Raj Singh" in text
