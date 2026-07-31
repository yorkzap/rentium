"""Regression coverage for conversation-owned RAMA attachment batches."""

from __future__ import annotations

import uuid

import pytest
from django.core.files.base import ContentFile

from rentium.rama.models import (
    RamaAttachment,
    RamaAttachmentBatch,
    RamaAudit,
    RamaUpload,
)
from rentium.rama.service import _conversation_attachment_focus

pytestmark = pytest.mark.django_db


def _said(landlord, conversation_id, text):
    return RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.USER_MESSAGE,
        content={"text": text},
    )


def _batch(landlord, conversation_id, count, prefix="photo"):
    batch = RamaAttachmentBatch.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        status=RamaAttachmentBatch.Status.SEALED,
    )
    rows = []
    for sequence in range(count):
        row = RamaAttachment(
            batch=batch,
            original_filename=f"{prefix}{sequence}.jpg",
            content_type="image/jpeg",
            sha256=f"{sequence:064d}",
            size=1,
            sequence=sequence,
        )
        row.original.save(
            f"{prefix}{sequence}.jpg",
            ContentFile(b"x"),
            save=True,
        )
        rows.append(row)
    _said(
        landlord,
        conversation_id,
        f"attach these\n\n[RAMA attachment batch {batch.pk}; exactly {count} file(s)]",
    )
    return batch, rows


@pytest.fixture
def other_landlord():
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


def test_focus_uses_only_the_explicit_conversation_batch(landlord):
    conversation = uuid.uuid4()
    _batch(landlord, uuid.uuid4(), 17, "old")
    batch, rows = _batch(landlord, conversation, 11, "new")

    focus = _conversation_attachment_focus(landlord, conversation)

    assert focus["attachment_batch_id"] == str(batch.pk)
    assert focus["attachment_ids"] == [str(row.pk) for row in rows]
    assert focus["pending_photo_count"] == 11
    assert "attachment_batch_id" in focus["instruction"]


def test_latest_correction_controls_document_intent(landlord):
    conversation = uuid.uuid4()
    _batch(landlord, conversation, 1)
    _said(landlord, conversation, "this is a Scotiabank mortgage statement")
    _said(landlord, conversation, "No, it isn't a document; it is a gallery photo")

    focus = _conversation_attachment_focus(landlord, conversation)

    assert focus["landlord_described_as_business_record"] is False
    assert "attach_photo_to_listing" in focus["instruction"]


def test_genuine_business_document_is_still_detected(landlord):
    conversation = uuid.uuid4()
    _batch(landlord, conversation, 1, "mortgage")
    _said(landlord, conversation, "the mortgage renewal letter for 950 McKenzie")

    focus = _conversation_attachment_focus(landlord, conversation)

    assert focus["landlord_described_as_business_record"] is True
    assert "catalog_business_document" in focus["instruction"]


def test_bare_photo_attachment_defaults_to_business_document_not_listing(landlord):
    """Empty/vague caption must NOT become 'looks like inspection photo'."""
    conversation = uuid.uuid4()
    _batch(landlord, conversation, 1, "file")
    # Only the synthetic attach note — no "receipt"/"invoice" words from landlord.
    focus = _conversation_attachment_focus(landlord, conversation)

    assert focus["landlord_described_as_business_record"] is True
    assert focus.get("landlord_claims_listing_photo") is False
    assert "catalog_business_document" in focus["instruction"]
    assert "Do NOT say this 'looks like a property/inspection photo'" in focus[
        "instruction"
    ]


def test_batches_are_landlord_scoped(landlord, other_landlord):
    conversation = uuid.uuid4()
    batch, _ = _batch(other_landlord, conversation, 2)
    _said(
        landlord,
        conversation,
        f"[RAMA attachment batch {batch.pk}; exactly 2 file(s)]",
    )

    assert _conversation_attachment_focus(landlord, conversation) == {}


def test_eleven_photo_batch_does_not_consume_seventeen_older_files(landlord):
    from rentium.properties.models import Property
    from rentium.rama import registry

    prop = Property.objects.create(
        landlord=landlord,
        name="McKenzie Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
    )
    old_batch, old_rows = _batch(landlord, uuid.uuid4(), 17, "old")
    new_batch, new_rows = _batch(landlord, uuid.uuid4(), 11, "garden")

    preview = registry.execute(
        "attach_photo_to_listing",
        {
            "property_query": prop.name,
            "attachment_batch_id": str(new_batch.pk),
        },
        landlord=landlord,
    )
    assert preview["preview"]["photos"] == 11
    assert preview["preview"]["attachment_ids"] == [str(row.pk) for row in new_rows]

    done = registry.execute(
        "attach_photo_to_listing",
        {
            "property_query": prop.name,
            "attachment_batch_id": str(new_batch.pk),
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done["photos_added"] == 11
    assert len(done["media"]) == 11
    assert RamaAttachment.objects.filter(
        batch=new_batch,
        status=RamaAttachment.Status.APPLIED,
    ).count() == 11
    assert RamaAttachment.objects.filter(
        batch=old_batch,
        status=RamaAttachment.Status.STAGED,
    ).count() == len(old_rows) == 17


def test_blank_and_all_legacy_upload_selection_are_refused(landlord):
    from rentium.properties.models import Property
    from rentium.rama import registry

    prop = Property.objects.create(
        landlord=landlord,
        name="Safe Gallery",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    RamaUpload.objects.create(
        landlord=landlord,
        image=ContentFile(b"x", name="unrelated.jpg"),
    )
    for upload_id in ("", "all"):
        result = registry.execute(
            "attach_photo_to_listing",
            {"property_query": prop.name, "upload_id": upload_id},
            landlord=landlord,
        )
        assert "explicit attachment_batch_id is required" in result["error"]


def test_specific_wrong_photo_can_be_removed_by_stable_handle(landlord):
    from rentium.properties.models import Property
    from rentium.rama import registry

    prop = Property.objects.create(
        landlord=landlord,
        name="Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
    )
    batch, _ = _batch(landlord, uuid.uuid4(), 1, "wrong-mortgage")
    attached = registry.execute(
        "attach_photo_to_listing",
        {
            "property_query": prop.name,
            "attachment_batch_id": str(batch.pk),
            "confirm": "yes",
        },
        landlord=landlord,
    )
    handle = attached["media"][0]["handle"]

    preview = registry.execute(
        "remove_photo_from_listing",
        {"property_query": prop.name, "media_handle": handle},
        landlord=landlord,
    )
    assert preview["needs_confirm"] is True
    done = registry.execute(
        "remove_photo_from_listing",
        {
            "property_query": prop.name,
            "media_handle": handle,
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done["removed"] is True
    assert done["remaining_media"] == []
