"""Conversation-scoped staging for files attached to RAMA chat messages."""

from __future__ import annotations

import hashlib
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from .models import RamaAttachment
from .models import RamaAttachmentBatch

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
    ".heic",
    ".heif",
}
FILE_REQUIRED = "A file is required."
FILE_TOO_LARGE = "File too large (max 15MB)."
UNSUPPORTED_FILE_TYPE = (
    "Unsupported file type. Use PDF, JPG, PNG, TIFF, WebP, HEIC, or HEIF."
)
FILES_REQUIRED = "At least one file is required."
BATCH_NOT_FOUND = "Attachment batch not found for this conversation."
BATCH_ALREADY_SENT = "That attachment batch has already been sent."
ATTACHMENT_NOT_FOUND = "Attachment not found."
SENT_ATTACHMENT_IMMUTABLE = (
    "Sent attachments can no longer be removed from the batch."
)
BATCH_EMPTY = "The attachment batch is empty."
BATCH_NOT_USABLE = "That attachment batch is not usable."


class AttachmentError(ValueError):
    pass


def _sha256(upload) -> str:
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


def _validate(upload) -> None:
    if not upload:
        raise AttachmentError(FILE_REQUIRED)
    if upload.size > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(FILE_TOO_LARGE)
    suffix = Path(upload.name or "").suffix.casefold()
    if suffix not in ALLOWED_EXTENSIONS:
        raise AttachmentError(UNSUPPORTED_FILE_TYPE)


def attachment_payload(attachment: RamaAttachment) -> dict:
    return {
        "id": str(attachment.pk),
        "name": attachment.original_filename,
        "content_type": attachment.content_type,
        "size": attachment.size,
        "sequence": attachment.sequence,
        "classification": attachment.classification,
        "status": attachment.status,
    }


def batch_payload(batch: RamaAttachmentBatch) -> dict:
    return {
        "batch_id": str(batch.pk),
        "conversation_id": str(batch.conversation_id),
        "status": batch.status,
        "attachments": [
            attachment_payload(row) for row in batch.attachments.order_by("sequence")
        ],
    }


@transaction.atomic
def stage_files(
    *,
    landlord,
    conversation_id,
    uploads,
    batch_id: str = "",
) -> RamaAttachmentBatch:
    uploads = list(uploads)
    if not uploads:
        raise AttachmentError(FILES_REQUIRED)
    for upload in uploads:
        _validate(upload)

    if batch_id:
        batch = (
            RamaAttachmentBatch.objects.select_for_update()
            .filter(
                pk=batch_id,
                landlord=landlord,
                conversation_id=conversation_id,
            )
            .first()
        )
        if batch is None:
            raise AttachmentError(BATCH_NOT_FOUND)
        if batch.status != RamaAttachmentBatch.Status.OPEN:
            raise AttachmentError(BATCH_ALREADY_SENT)
    else:
        batch = RamaAttachmentBatch.objects.create(
            landlord=landlord,
            conversation_id=conversation_id,
        )

    sequence = (
        batch.attachments.order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    next_sequence = (sequence + 1) if sequence is not None else 0
    for offset, upload in enumerate(uploads):
        RamaAttachment.objects.create(
            batch=batch,
            original=upload,
            original_filename=(upload.name or "attachment")[:255],
            content_type=(getattr(upload, "content_type", "") or "")[:160],
            sha256=_sha256(upload),
            size=upload.size,
            sequence=next_sequence + offset,
        )
    return batch


@transaction.atomic
def remove_staged_attachment(*, landlord, attachment_id) -> RamaAttachmentBatch:
    attachment = (
        RamaAttachment.objects.select_related("batch")
        .select_for_update()
        .filter(pk=attachment_id, batch__landlord=landlord)
        .first()
    )
    if attachment is None:
        raise AttachmentError(ATTACHMENT_NOT_FOUND)
    if attachment.batch.status != RamaAttachmentBatch.Status.OPEN:
        raise AttachmentError(SENT_ATTACHMENT_IMMUTABLE)
    batch = attachment.batch
    attachment.delete()
    return batch


@transaction.atomic
def seal_batch(*, landlord, conversation_id, batch_id) -> RamaAttachmentBatch:
    batch = (
        RamaAttachmentBatch.objects.select_for_update()
        .filter(
            pk=batch_id,
            landlord=landlord,
            conversation_id=conversation_id,
        )
        .first()
    )
    if batch is None:
        raise AttachmentError(BATCH_NOT_FOUND)
    if not batch.attachments.exists():
        raise AttachmentError(BATCH_EMPTY)
    if batch.status == RamaAttachmentBatch.Status.OPEN:
        batch.status = RamaAttachmentBatch.Status.SEALED
        batch.sealed_at = timezone.now()
        batch.save(update_fields=["status", "sealed_at"])
    elif batch.status not in {
        RamaAttachmentBatch.Status.SEALED,
        RamaAttachmentBatch.Status.PROCESSING,
        RamaAttachmentBatch.Status.COMPLETED,
    }:
        raise AttachmentError(BATCH_NOT_USABLE)
    return batch


def resolve_batch_attachments(
    *,
    landlord,
    batch_id: str,
    attachment_ids: list[str] | None = None,
    allowed_statuses: tuple[str, ...] = (
        RamaAttachment.Status.STAGED,
        RamaAttachment.Status.CLASSIFIED,
    ),
) -> tuple[RamaAttachmentBatch | None, list[RamaAttachment]]:
    batch = (
        RamaAttachmentBatch.objects.filter(pk=batch_id, landlord=landlord)
        .prefetch_related("attachments")
        .first()
    )
    if batch is None:
        return None, []
    queryset = batch.attachments.filter(status__in=allowed_statuses).order_by(
        "sequence",
    )
    if attachment_ids:
        requested = {str(value) for value in attachment_ids}
        queryset = queryset.filter(pk__in=requested)
    return batch, list(queryset)


def batch_chat_note(batch: RamaAttachmentBatch) -> str:
    rows = list(batch.attachments.order_by("sequence"))
    labels = ", ".join(
        f"{row.sequence + 1}:{row.pk}:{row.original_filename}" for row in rows
    )
    return (
        f"\n\n[RAMA attachment batch {batch.pk}; exactly {len(rows)} file(s); "
        f"items={labels}]\n"
        "Use only this batch for this request. Determine whether the landlord "
        "means property media or business documents from their words. For listing "
        "media call attach_photo_to_listing with attachment_batch_id set to "
        "this batch ID. Never substitute older uploads or an unmentioned batch."
    )
