"""Application-owned operations for listing media.

REST endpoints and RAMA adapters both use these functions so ordering, primary
selection, ownership checks, and deletion semantics cannot drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from django.core.files.base import ContentFile
from django.db import transaction

from .models import Property
from .models import PropertyImage

if TYPE_CHECKING:
    from collections.abc import Iterable

INVALID_MEDIA_HANDLE = "Media handle must be 'primary' or 'gallery:<id>'."
INCOMPLETE_REORDER = "Reorder must include every current gallery handle once."


class PropertyMediaError(ValueError):
    pass


def media_manifest(property_obj: Property) -> list[dict]:
    rows: list[dict] = []
    if property_obj.primary_image:
        rows.append(
            {
                "handle": "primary",
                "kind": "primary",
                "url": property_obj.primary_image.url,
                "filename": Path(property_obj.primary_image.name).name,
                "order": -1,
            },
        )
    rows.extend(
        {
            "handle": f"gallery:{image.pk}",
            "kind": "gallery",
            "id": image.pk,
            "url": image.image.url,
            "filename": Path(image.image.name).name,
            "caption": image.caption,
            "order": image.order,
        }
        for image in property_obj.property_images.order_by("order", "created_at")
    )
    for index, row in enumerate(rows, start=1):
        row["selection_number"] = index
    return rows


@transaction.atomic
def attach_media(
    *,
    property_obj: Property,
    sources: Iterable,
    make_first_primary: bool = False,
) -> list[dict]:
    """Copy ordered file-like sources into listing media.

    A source needs ``name``, ``open()``, ``read()``, and ``close()`` (Django
    FieldFile satisfies that contract).  Gallery is the safe default.
    """
    property_obj = Property.objects.select_for_update().get(pk=property_obj.pk)
    next_order = (
        property_obj.property_images.order_by("-order")
        .values_list("order", flat=True)
        .first()
    )
    next_order = (next_order + 1) if next_order is not None else 0
    attached: list[dict] = []
    for index, source in enumerate(sources):
        source.open("rb")
        try:
            data = source.read()
            basename = Path(source.name).name
        finally:
            source.close()
        if make_first_primary and index == 0:
            property_obj.primary_image.save(
                basename,
                ContentFile(data),
                save=True,
            )
            attached.append({"handle": "primary", "filename": basename})
            continue
        image = PropertyImage(property=property_obj, order=next_order)
        image.image.save(basename, ContentFile(data), save=True)
        attached.append(
            {
                "handle": f"gallery:{image.pk}",
                "id": image.pk,
                "filename": basename,
            },
        )
        next_order += 1
    return attached


@transaction.atomic
def remove_media(*, property_obj: Property, handle: str) -> dict:
    return remove_media_many(property_obj=property_obj, handles=[handle])[0]


@transaction.atomic
def remove_media_many(*, property_obj: Property, handles: list[str]) -> list[dict]:
    """Remove an exact set of media handles together, retaining storage files."""
    property_obj = Property.objects.select_for_update().get(pk=property_obj.pk)
    clean_handles = list(dict.fromkeys(str(handle or "").strip() for handle in handles))
    if not clean_handles or any(not handle for handle in clean_handles):
        raise PropertyMediaError(INVALID_MEDIA_HANDLE)

    gallery = {
        f"gallery:{row.pk}": row
        for row in property_obj.property_images.select_for_update()
    }
    available = set(gallery)
    if property_obj.primary_image:
        available.add("primary")
    missing = [handle for handle in clean_handles if handle not in available]
    if missing:
        message = "Media not found on this listing: " + ", ".join(missing)
        raise PropertyMediaError(message)

    removed: list[dict] = []
    if "primary" in clean_handles:
        previous = property_obj.primary_image.name
        property_obj.primary_image = None
        property_obj.save(update_fields=["primary_image", "updated_at"])
        removed.append({"removed": "primary", "storage_name": previous})
    for handle in clean_handles:
        if handle == "primary":
            continue
        image = gallery[handle]
        previous = image.image.name
        image.delete()
        removed.append({"removed": handle, "storage_name": previous})
    return removed


@transaction.atomic
def reorder_gallery(*, property_obj: Property, handles: list[str]) -> list[dict]:
    property_obj = Property.objects.select_for_update().get(pk=property_obj.pk)
    images = {
        f"gallery:{row.pk}": row
        for row in property_obj.property_images.select_for_update()
    }
    if set(handles) != set(images):
        raise PropertyMediaError(INCOMPLETE_REORDER)
    for order, handle in enumerate(handles):
        row = images[handle]
        row.order = order
        row.save(update_fields=["order"])
    return media_manifest(property_obj)
