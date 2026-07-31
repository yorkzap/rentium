"""
Property image semantics: a listing with ANY photo (primary hero or gallery)
must never be treated as photo-less — not by publish_blockers, not by public
cards, and not by RAMA's grounded rows (tested in rama/tests.py).
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from rentium.properties.models import PropertyImage

pytestmark = pytest.mark.django_db

# Smallest valid GIF — enough for ImageField storage (no form validation here).
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

PHOTO_BLOCKER = "Add at least one photo. Nobody enquires about a grey box."


def _img(name="photo.gif"):
    return SimpleUploadedFile(name, TINY_GIF, content_type="image/gif")


def _gallery(prop, count=1):
    return [
        PropertyImage.objects.create(property=prop, image=_img(f"g{i}.gif"), order=i)
        for i in range(count)
    ]


# ------------------------------------------------------- publish_blockers
def test_photo_blocker_when_no_images_at_all(bc_property):
    assert PHOTO_BLOCKER in bc_property.publish_blockers()


def test_photo_blocker_satisfied_by_primary_image(bc_property):
    bc_property.primary_image = _img()
    bc_property.save()
    assert PHOTO_BLOCKER not in bc_property.publish_blockers()


def test_photo_blocker_satisfied_by_gallery_only(bc_property):
    _gallery(bc_property)
    assert PHOTO_BLOCKER not in bc_property.publish_blockers()


# ------------------------------------------------------------ image_count
@pytest.mark.parametrize(
    ("primary", "gallery", "expected"),
    [(False, 0, 0), (True, 0, 1), (False, 2, 2), (True, 3, 4)],
)
def test_image_count(bc_property, primary, gallery, expected):
    if primary:
        bc_property.primary_image = _img()
        bc_property.save()
    _gallery(bc_property, gallery)
    assert bc_property.image_count == expected
    assert bc_property.has_gallery_images is (gallery > 0)


def test_image_count_honours_gallery_annotation(bc_property, django_assert_num_queries):
    from django.db.models import Count

    from rentium.properties.models import Property

    _gallery(bc_property, 2)
    prop = (
        Property.objects.filter(pk=bc_property.pk)
        .annotate(_gallery_count=Count("property_images", distinct=True))
        .get()
    )
    with django_assert_num_queries(0):  # annotation used, no extra query
        assert prop.image_count == 2


# ---------------------------------------------------------- display_image
def test_display_image_prefers_primary(bc_property):
    bc_property.primary_image = _img("hero.gif")
    bc_property.save()
    _gallery(bc_property)
    assert bc_property.display_image.name == bc_property.primary_image.name


def test_display_image_falls_back_to_first_gallery_image(bc_property):
    images = _gallery(bc_property, 2)
    assert bc_property.display_image.name == images[0].image.name


def test_display_image_none_when_no_images(bc_property):
    assert bc_property.display_image is None


def test_property_media_api_lists_and_deletes_exact_primary_or_gallery(
    landlord,
    bc_property,
):
    bc_property.primary_image = _img("hero.gif")
    bc_property.save()
    gallery = _gallery(bc_property, 2)
    client = APIClient()
    client.force_authenticate(user=landlord.user)

    manifest = client.get(f"/api/properties/{bc_property.pk}/media/")
    assert manifest.status_code == 200
    assert [row["handle"] for row in manifest.json()] == [
        "primary",
        f"gallery:{gallery[0].pk}",
        f"gallery:{gallery[1].pk}",
    ]
    assert [row["selection_number"] for row in manifest.json()] == [1, 2, 3]

    removed_primary = client.delete(
        f"/api/properties/{bc_property.pk}/media/primary/",
    )
    assert removed_primary.status_code == 200
    bc_property.refresh_from_db()
    assert not bc_property.primary_image
    assert bc_property.property_images.count() == 2

    removed_gallery = client.delete(
        f"/api/properties/{bc_property.pk}/media/gallery%3A{gallery[0].pk}/",
    )
    assert removed_gallery.status_code == 200
    assert not bc_property.property_images.filter(pk=gallery[0].pk).exists()
    assert bc_property.property_images.filter(pk=gallery[1].pk).exists()


def test_group_membership_keeps_common_area_associations_synchronized(landlord):
    from rentium.properties.models import Property, PropertyArea, PropertyGroup
    from rentium.properties.services import assign_room_to_group
    from rentium.properties.services import create_group_common_area

    first = Property.objects.create(
        landlord=landlord,
        name="Group Room A",
        address="1 Shared St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    second = Property.objects.create(
        landlord=landlord,
        name="Group Room B",
        address="1 Shared St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    third = Property.objects.create(
        landlord=landlord,
        name="Group Room C",
        address="1 Shared St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    group = PropertyGroup.objects.create(landlord=landlord, name="Shared Group")
    other_group = PropertyGroup.objects.create(landlord=landlord, name="Other Group")
    assign_room_to_group(first, group)
    assign_room_to_group(second, group)
    area, _ = create_group_common_area(
        group,
        area_type=PropertyArea.AreaType.KITCHEN,
        shared_with_landlord=False,
    )
    assert set(area.shared_by.values_list("pk", flat=True)) == {first.pk, second.pk}

    assign_room_to_group(third, group)
    area.refresh_from_db()
    assert set(area.shared_by.values_list("pk", flat=True)) == {
        first.pk,
        second.pk,
        third.pk,
    }

    assign_room_to_group(second, other_group)
    area.refresh_from_db()
    assert set(area.shared_by.values_list("pk", flat=True)) == {first.pk, third.pk}

    assign_room_to_group(first, None)
    area.refresh_from_db()
    assert area.property_id == third.pk
    assert set(area.shared_by.values_list("pk", flat=True)) == {third.pk}
    assert area.is_group_common is True
