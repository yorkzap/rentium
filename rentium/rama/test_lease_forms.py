"""
RAMA's side of lease form packs.

Two things are being protected here. First, the registration contract: a tool
that is registered but missing from TOOL_META inherits a default that would let
it behave differently than intended, and one missing from the capability
contract is invisible to the landlord-facing General. Second — and this is the
one that matters — that RAMA cannot decide what an unfamiliar form is FOR. That
choice determines whether an unsigned PDF holds up somebody's tenancy, and it
belongs to the landlord.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.core.management import call_command

from rentium.leases.lease_forms import FormStage
from rentium.leases.lease_forms import LeaseForm
from rentium.leases.lease_forms import LeaseFormTemplate
from rentium.leases.models import Lease
from rentium.leases.models import LeaseTenant
from rentium.rama import registry
# The landlord typing in a form's mandatory content, shared with the
# service tests so both exercise the same send-time guard.
from rentium.leases.test_lease_forms import _fill_required
from rentium.rama.landlord_capabilities import manage_lease_forms

pytestmark = pytest.mark.django_db


@pytest.fixture
def catalogue():
    call_command("seed_lease_forms", verbosity=0)
    return LeaseFormTemplate.objects.filter(landlord__isnull=True)


@pytest.fixture
def lease(landlord, bc_property, tenant):
    lease = Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.ACTIVE,
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
# Registration contract
# ---------------------------------------------------------------------------


def test_the_tool_is_registered_with_a_schema_the_model_can_use():
    tool = registry.REGISTRY["manage_lease_forms"]
    assert tool.description
    assert tool.parameters["required"] == ["action"]
    # `landlord` is injected server-side and must never be selectable.
    assert "landlord" not in tool.parameters["properties"]
    assert "confirm" in tool.parameters["properties"]
    # Weak models get almost nothing from a bare {"type": "string"}.
    assert tool.parameters["properties"]["stage"]["description"]


def test_it_is_classified_as_a_legal_write_that_confirms_on_its_own():
    from rentium.rama.tool_meta import TOOL_META
    from rentium.rama.tool_meta import Autonomy

    meta = TOOL_META["manage_lease_forms"]
    assert meta.risk == "legal"
    assert meta.own_confirm is True
    # It attaches documents that can hold up or end a tenancy. Never automatic.
    assert meta.autonomy == Autonomy.NEVER


def test_the_general_can_reach_it_and_retrieval_surfaces_it():
    from rentium.rama.capabilities import select_tool_schemas
    from rentium.rama.capability_contract import general_tool_names
    from rentium.rama.roles import READ_TOOLS

    assert "manage_lease_forms" in general_tool_names(READ_TOOLS)

    schemas = registry.tool_schemas()
    for message in (
        "attach the RTB-8 to the Oak Ave lease",
        "I need Sarah to sign a pet addendum",
        "send this form for signature",
    ):
        names = [s["name"] for s in select_tool_schemas(message, schemas)]
        assert "manage_lease_forms" in names, message


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_the_catalog_says_which_forms_cannot_be_used_yet(landlord, catalogue):
    result = manage_lease_forms(landlord, action="catalog")
    by_code = {row["form_code"]: row for row in result["forms"]}

    assert by_code["BC_RTB8"]["available"] is True
    assert by_code["BC_RTB1"]["available"] is False
    assert by_code["BC_RTB1"]["note"]
    # RAMA has to be able to say what a form is for, in words.
    assert "end the tenancy" in by_code["BC_RTB8"]["what_it_is_for"]


def test_an_unknown_action_is_rejected_with_the_list(landlord):
    assert "action must be one of" in manage_lease_forms(landlord, action="frobnicate")["error"]


def test_list_reports_who_is_still_owed_a_signature(landlord, lease, catalogue):
    from rentium.leases import form_services as svc

    form = svc.attach_form(lease, catalogue.get(code="BC_RTB8"))
    svc.send_form(_fill_required(form), notify=False)
    svc.sign_form(form.signers.first(), typed_name="Raj Singh")

    result = manage_lease_forms(landlord, action="list", lease_query="Oak Ave Suite B")
    row = result["forms"][0]

    assert row["signed_by"] == ["Raj Singh"] or row["signed_by"]
    assert row["waiting_on"]
    assert row["status"] == LeaseForm.Status.PARTIALLY_SIGNED


def test_lease_forms_show_up_in_list_documents(landlord, lease, catalogue):
    """Closes the parity gap: RAMA could read LeaseDocument but not form packs."""
    from rentium.leases import form_services as svc
    from rentium.rama.domain_reads import list_documents

    svc.attach_form(lease, catalogue.get(code="BC_RTB8"))

    rows = list_documents(landlord)["documents"]
    forms = [row for row in rows if row["kind"] == "lease_form"]
    assert len(forms) == 1
    assert forms[0]["form_code"] == "BC_RTB8"
    assert forms[0]["stage"] == FormStage.MOVE_OUT


# ---------------------------------------------------------------------------
# Writes are previewed, never self-approved
# ---------------------------------------------------------------------------


def test_attaching_previews_first_and_writes_nothing(landlord, lease, catalogue):
    result = manage_lease_forms(
        landlord, action="attach", lease_query="Oak Ave Suite B", form_code="BC_RTB8"
    )
    assert result["needs_confirm"] is True
    assert result["preview"]["form"].startswith("Mutual Agreement")
    assert lease.lease_forms.count() == 0, "a preview must not write"


def test_confirming_attaches_it(landlord, lease, catalogue):
    result = manage_lease_forms(
        landlord,
        action="attach",
        lease_query="Oak Ave Suite B",
        form_code="BC_RTB8",
        confirm="yes",
    )
    # "created", not "attached": service._is_write_result reads these flags to
    # decide whether a turn wrote anything, and `attached` is already a
    # descriptive (non-write) flag elsewhere.
    assert result["created"] is True
    assert lease.lease_forms.count() == 1
    assert result["stage"] == FormStage.MOVE_OUT


def test_the_write_guard_can_see_every_flag_this_tool_sets(landlord, lease, catalogue):
    """A write the guard can't see is reported to the landlord as not happening."""
    from rentium.rama.service import _is_write_result

    attached = manage_lease_forms(
        landlord,
        action="attach",
        lease_query="Oak Ave Suite B",
        form_code="BC_RTB8",
        confirm="yes",
    )
    assert _is_write_result(attached)

    from rentium.leases.lease_forms import LeaseForm as LeaseFormModel

    _fill_required(LeaseFormModel.objects.get(pk=attached["form_id"]))
    sent = manage_lease_forms(
        landlord, action="send", form_id=attached["form_id"], confirm="yes"
    )
    assert _is_write_result(sent)

    voided = manage_lease_forms(
        landlord, action="void", form_id=attached["form_id"], confirm="yes"
    )
    assert _is_write_result(voided)

    # And a preview must NOT read as a write.
    assert not _is_write_result(
        manage_lease_forms(
            landlord, action="attach", lease_query="Oak Ave Suite B", form_code="BC_RTB8"
        )
    )


def test_sending_previews_who_will_be_emailed(landlord, lease, catalogue):
    from rentium.leases import form_services as svc

    form = svc.attach_form(lease, catalogue.get(code="BC_RTB8"))
    result = manage_lease_forms(landlord, action="send", form_id=str(form.pk))

    assert result["needs_confirm"] is True
    assert result["preview"]["emails"]
    # Signer rows exist from attach time now, so the landlord can sign before
    # sending. What a preview must not do is SEND: nothing is stamped as sent,
    # so no link is live and no email has gone anywhere.
    assert form.signers.exists()
    assert not form.signers.filter(sent_at__isnull=False).exists()
    assert not any(signer.token_is_live for signer in form.signers.all())


def test_a_form_that_is_not_shipped_cannot_be_attached(landlord, lease, catalogue):
    result = manage_lease_forms(
        landlord,
        action="attach",
        lease_query="Oak Ave Suite B",
        form_code="BC_RTB1",
        confirm="yes",
    )
    assert "isn't shipped yet" in result["error"]
    assert lease.lease_forms.count() == 0


def test_an_ambiguous_form_name_refuses_rather_than_picking(landlord, lease, catalogue):
    result = manage_lease_forms(
        landlord, action="attach", lease_query="Oak Ave Suite B", form_code="Tenancy"
    )
    assert "More than one form matches" in result["error"]


def test_you_cannot_touch_another_landlords_form(landlord, lease, catalogue):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    stranger = LandlordProfile.objects.create(user=UserFactory())
    result = manage_lease_forms(
        stranger, action="list", lease_query="Oak Ave Suite B"
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# The thing RAMA must never decide
# ---------------------------------------------------------------------------


def test_an_unclassified_form_produces_a_question_not_a_guess(landlord, lease, catalogue):
    """The stage decides whether an unsigned PDF blocks a tenancy. Ask."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from rentium.leases import form_services as svc

    rtb8 = catalogue.get(code="BC_RTB8")
    rtb8.file.open("rb")
    try:
        data = rtb8.file.read()
    finally:
        rtb8.file.close()
    template, _ = svc.upload_template(
        landlord,
        SimpleUploadedFile("mystery.pdf", data, content_type="application/pdf"),
        name="Mystery Form",
    )

    result = manage_lease_forms(
        landlord,
        action="attach",
        lease_query="Oak Ave Suite B",
        form_code="Mystery Form",
        confirm="yes",  # even a confirmed call must stop and ask
    )

    assert result["needs_input"] is True
    assert "VERBATIM" in result["relay_instruction"]
    assert "signed with the lease" in result["question_for_user"]
    # It may offer its reading — but it is offered, not applied.
    assert result["suggested_stage"] == FormStage.MOVE_OUT
    assert lease.lease_forms.count() == 0
    template.refresh_from_db()
    assert template.stage == FormStage.UNCLASSIFIED


def test_classify_is_what_promotes_a_suggestion_into_a_fact(landlord, catalogue):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from rentium.leases import form_services as svc

    rtb8 = catalogue.get(code="BC_RTB8")
    rtb8.file.open("rb")
    try:
        data = rtb8.file.read()
    finally:
        rtb8.file.close()
    template, _ = svc.upload_template(
        landlord,
        SimpleUploadedFile("mystery.pdf", data, content_type="application/pdf"),
        name="Mystery Form",
    )

    preview = manage_lease_forms(
        landlord, action="classify", form_code="Mystery Form", stage="MOVE_OUT"
    )
    assert preview["needs_confirm"] is True
    template.refresh_from_db()
    assert template.stage == FormStage.UNCLASSIFIED

    manage_lease_forms(
        landlord,
        action="classify",
        form_code="Mystery Form",
        stage="end of tenancy",  # the words a landlord would actually say
        confirm="yes",
    )
    template.refresh_from_db()
    assert template.stage == FormStage.MOVE_OUT


def test_a_system_forms_purpose_cannot_be_reassigned(landlord, catalogue):
    """One account must not change what RTB-8 means for every other account."""
    result = manage_lease_forms(
        landlord,
        action="classify",
        form_code="BC_RTB8",
        stage="WITH_LEASE",
        confirm="yes",
    )
    assert "error" in result
    assert catalogue.get(code="BC_RTB8").stage == FormStage.MOVE_OUT


# ---------------------------------------------------------------------------
# Attachment routing: the third branch
# ---------------------------------------------------------------------------


def test_a_pdf_sent_for_signing_routes_to_forms_not_the_expense_inbox():
    from rentium.rama.attachment_services import looks_like_lease_form

    assert looks_like_lease_form("get Sarah to sign this")
    assert looks_like_lease_form("here's the RTB-8 for the Oak Ave tenancy")
    assert looks_like_lease_form("add this addendum to her lease")

    # Still the default for everything that is not about signing.
    assert not looks_like_lease_form("here's the plumber's invoice")
    assert not looks_like_lease_form("")


def test_the_chat_note_tells_the_model_which_of_the_three_paths_to_take(landlord):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from rentium.rama.attachment_services import batch_chat_note
    from rentium.rama.attachment_services import stage_files

    batch = stage_files(
        landlord=landlord,
        conversation_id="11111111-1111-1111-1111-111111111111",
        uploads=[SimpleUploadedFile("form.pdf", b"%PDF-1.4 x", "application/pdf")],
    )

    signing = batch_chat_note(batch, caption="can you get Sarah to sign this?")
    assert "manage_lease_forms" in signing
    assert "Do NOT call catalog_business_document" in signing

    expense = batch_chat_note(batch, caption="plumber's invoice for the McKenzie place")
    assert "catalog_business_document" in expense
    assert "manage_lease_forms" not in expense


def test_the_focus_block_carries_the_same_three_way_choice():
    from rentium.rama.service import _attachment_routing_instruction

    signing = _attachment_routing_instruction(lease_form=True, business_record=True)
    assert "manage_lease_forms" in signing
    assert "never choose a stage yourself" in signing

    default = _attachment_routing_instruction(lease_form=False, business_record=True)
    assert "catalog_business_document" in default

    media = _attachment_routing_instruction(lease_form=False, business_record=False)
    assert "attach_photo_to_listing" in media
