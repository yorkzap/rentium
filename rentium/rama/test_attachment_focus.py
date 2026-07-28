"""
Attaching a burst of photos to a listing.

From a real transcript. Twelve images were uploaded and asked to be added to a
listing's gallery. RAMA offered to file all of them as business documents; told
"these are NOT business documents, these are gallery images", it re-previewed
the SAME thing; then it offered 2 photos, then 1.

Three separate faults compounded:

1. `landlord_described_as_business_record` was a bare substring scan, so the
   sentence "these are not business DOCUMENTS" matched "document" and set the
   flag to True. Correcting RAMA reinforced the error being corrected.
2. Upload ids were scraped out of the last 12 user messages. A burst of photos
   scrolled out of that window as the conversation continued, so 12 pending
   uploads were presented as 2, then 1 — while all 12 sat unused in the table.
3. Nothing told the model how many photos were actually pending, or that one
   call with a blank upload_id attaches all of them.
"""

from __future__ import annotations

import uuid

import pytest
from django.core.files.base import ContentFile

from rentium.rama.models import RamaAudit, RamaUpload
from rentium.rama.service import _conversation_attachment_focus

pytestmark = pytest.mark.django_db


def _upload(landlord, name="photo.jpg"):
    upload = RamaUpload(landlord=landlord)
    upload.image.save(name, ContentFile(b"x"), save=False)
    upload.save()
    return upload


def _said(landlord, conversation_id, text):
    return RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.USER_MESSAGE,
        content={"text": text},
    )


# ------------------------------------------------------------ the negation
def test_saying_they_are_not_business_documents_is_believed(landlord):
    conv = uuid.uuid4()
    _upload(landlord)
    _said(landlord, conv, "[The landlord attached a photo, upload_id=x] here")
    _said(
        landlord,
        conv,
        "No, these are not business documents, these are just images i want "
        "to attach to mccaughey basement as gallery image dude",
    )

    focus = _conversation_attachment_focus(landlord, conv)
    assert focus["landlord_described_as_business_record"] is False
    assert "attach_photo_to_listing" in focus["instruction"]


@pytest.mark.parametrize(
    "denial",
    [
        "these are not documents",
        "it isn't a receipt",
        "they aren't invoices",
        "no this is not mail",
        "not paperwork, just pics",
    ],
)
def test_every_shape_of_denial_is_believed(landlord, denial):
    conv = uuid.uuid4()
    _upload(landlord)
    _said(landlord, conv, denial)
    focus = _conversation_attachment_focus(landlord, conv)
    assert focus["landlord_described_as_business_record"] is False


def test_asking_for_the_gallery_is_believed(landlord):
    conv = uuid.uuid4()
    _upload(landlord)
    _said(landlord, conv, "attach those as gallery images to McCaughey Basement")
    focus = _conversation_attachment_focus(landlord, conv)
    assert focus["landlord_described_as_business_record"] is False


def test_a_genuine_business_document_is_still_detected(landlord):
    """The fix must not stop RAMA filing photographed mail."""
    conv = uuid.uuid4()
    _upload(landlord)
    _said(landlord, conv, "here is the Scotiabank mortgage statement for the house")
    focus = _conversation_attachment_focus(landlord, conv)
    assert focus["landlord_described_as_business_record"] is True
    assert "catalog_business_document" in focus["instruction"]


def test_the_latest_message_wins(landlord):
    """Called it a document, then corrected — the correction must win."""
    conv = uuid.uuid4()
    _upload(landlord)
    _said(landlord, conv, "here is a receipt")
    _said(landlord, conv, "actually these are not receipts, just gallery photos")
    focus = _conversation_attachment_focus(landlord, conv)
    assert focus["landlord_described_as_business_record"] is False


# ----------------------------------------------------- the vanishing photos
def test_a_burst_of_twelve_photos_stays_twelve(landlord):
    conv = uuid.uuid4()
    for i in range(12):
        _upload(landlord, f"photo{i}.jpg")
    _said(landlord, conv, "attach these as gallery images")

    focus = _conversation_attachment_focus(landlord, conv)
    assert focus["pending_photo_count"] == 12
    assert len(focus["unresolved_upload_ids"]) == 12


def test_photos_survive_a_long_conversation(landlord):
    """The regression: enough follow-up messages to push the upload markers
    out of the 12-row history window used to drop them to 2, then 1."""
    conv = uuid.uuid4()
    for i in range(12):
        _upload(landlord, f"photo{i}.jpg")
    _said(landlord, conv, "[The landlord attached a photo, upload_id=x] 12 pics")
    for i in range(20):
        _said(landlord, conv, f"follow-up message number {i}")

    focus = _conversation_attachment_focus(landlord, conv)
    assert focus["pending_photo_count"] == 12


def test_used_photos_are_not_offered_again(landlord):
    from django.utils import timezone

    conv = uuid.uuid4()
    spent = _upload(landlord)
    _upload(landlord)
    spent.used_at = timezone.now()
    spent.save(update_fields=["used_at"])
    _said(landlord, conv, "attach these")

    focus = _conversation_attachment_focus(landlord, conv)
    assert focus["pending_photo_count"] == 1
    assert str(spent.pk) not in focus["unresolved_upload_ids"]


def test_uploads_are_landlord_scoped(landlord, other_landlord):
    conv = uuid.uuid4()
    _upload(other_landlord)
    _said(landlord, conv, "attach these")
    assert _conversation_attachment_focus(landlord, conv) == {}


def test_the_instruction_says_attach_them_in_one_call(landlord):
    """The model was calling the tool once per photo, or guessing a subset."""
    conv = uuid.uuid4()
    for i in range(12):
        _upload(landlord, f"photo{i}.jpg")
    _said(landlord, conv, "put these in the gallery")

    focus = _conversation_attachment_focus(landlord, conv)
    assert "ONCE" in focus["instruction"]
    assert "BLANK" in focus["instruction"]
    assert "12" in focus["instruction"]


@pytest.fixture
def other_landlord():
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


def test_twelve_photos_attach_in_a_single_call(landlord):
    """End to end: the transcript's actual goal. One call, blank upload_id,
    all twelve placed — not 5 catalog steps, not 2 photos, not 1."""
    from rentium.properties.models import Property
    from rentium.rama import registry

    prop = Property.objects.create(
        landlord=landlord,
        name="McCaughey Basement",
        address="5654 McCaughey Street",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    for i in range(12):
        _upload(landlord, f"photo{i}.jpg")

    preview = registry.execute(
        "attach_photo_to_listing",
        {"property_query": "McCaughey Basement"},
        landlord=landlord,
    )
    assert preview["preview"]["photos"] == 12

    done = registry.execute(
        "attach_photo_to_listing",
        {"property_query": "McCaughey Basement", "confirm": "yes"},
        landlord=landlord,
    )
    assert done["photos_added"] == 12
    assert RamaUpload.objects.filter(landlord=landlord, used_at__isnull=True).count() == 0
    assert prop.image_count >= 12
