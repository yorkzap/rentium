"""Document ingestion, OCR, classification, filing, and accounting services."""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import tempfile
from datetime import date
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from rentium.ledger.models import ExpenseCategory
from rentium.ledger import services as ledger_services
from rentium.ledger.services import post_expense
from rentium.properties.models import PropertyHolding

from .models import RamaDocument
from .models import RamaDocumentEvent
from .links import url_for_path

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
ALLOWED_MEDIA_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
    "image/heic",
    "image/heif",
}


class DocumentError(ValueError):
    pass


class DuplicateExpenseError(DocumentError):
    """This receipt may document a cost that is already on the books.

    Carries the candidates so the caller can offer "attach to that one" instead
    of only "post it anyway" — linking is almost always the right answer, and a
    dialog that offers no way to say so trains people to click through.
    """

    def __init__(self, message, *, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


def _link_to_existing_expense(document, entry_id: str, actor):
    """Attach this document to an expense already on the books.

    Posts nothing. The receipt becomes the evidence for an entry that was
    recorded from a message, which is the outcome someone actually wants when
    they photograph a receipt for a cost they already mentioned.
    """
    from rentium.ledger.models import EntryType, LedgerEntry

    entry = LedgerEntry.objects.filter(
        landlord=document.landlord, pk=entry_id, entry_type=EntryType.EXPENSE
    ).first()
    if entry is None:
        raise DocumentError(f"No expense {entry_id!r} on this portfolio to attach to.")
    existing = getattr(entry, "source_document", None)
    if existing is not None and existing.pk != document.pk:
        raise DocumentError(
            f"That expense already has {existing.original_filename!r} attached."
        )
    if document.amount and entry.amount != document.amount:
        # Not fatal — a receipt total can legitimately differ from what was
        # entered — but it must be said rather than silently accepted.
        _event(
            document,
            RamaDocumentEvent.Kind.CLARIFIED,
            actor=actor,
            note=(
                f"Linked to an expense of {entry.amount} while this document "
                f"reads {document.amount}."
            ),
        )
    return entry


def _event(document, kind, *, actor=None, **detail):
    return RamaDocumentEvent.objects.create(
        document=document, kind=kind, actor=actor, detail=detail
    )


def _read_upload(upload) -> bytes:
    upload.seek(0)
    data = upload.read(MAX_DOCUMENT_BYTES + 1)
    upload.seek(0)
    if not data:
        raise DocumentError("The uploaded file is empty.")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentError("Document is too large (maximum 25 MB).")
    return data


def ingest_document(*, landlord, upload, created_by=None) -> tuple[RamaDocument, bool]:
    """Stage a supported document and enqueue its deterministic processing."""
    data = _read_upload(upload)
    media_type = (getattr(upload, "content_type", "") or "").lower()
    suffix = Path(upload.name).suffix.lower()
    if not media_type:
        media_type = {
            ".pdf": "application/pdf",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
            ".webp": "image/webp",
            ".heic": "image/heic",
            ".heif": "image/heif",
        }.get(suffix, "")
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise DocumentError("Use a PDF, JPG, PNG, TIFF, WebP, or HEIC file.")

    digest = hashlib.sha256(data).hexdigest()
    existing = RamaDocument.objects.filter(landlord=landlord, sha256=digest).first()
    if existing:
        return existing, False

    safe_name = Path(upload.name).name[:255]
    with transaction.atomic():
        document = RamaDocument(
            landlord=landlord,
            original_filename=safe_name,
            media_type=media_type,
            byte_size=len(data),
            sha256=digest,
            created_by=created_by,
        )
        document.original_file.save(safe_name, ContentFile(data), save=False)
        document.full_clean()
        document.save()
        _event(
            document,
            RamaDocumentEvent.Kind.UPLOADED,
            actor=created_by,
            filename=safe_name,
            sha256=digest,
        )
    return document, True


def _pdf_and_text(document: RamaDocument) -> tuple[bytes, str]:
    document.original_file.open("rb")
    try:
        source = document.original_file.read()
    finally:
        document.original_file.close()

    try:
        from PIL import Image
    except ImportError as exc:
        raise DocumentError("Document conversion support is not installed.") from exc

    with tempfile.TemporaryDirectory(prefix="rentium-ocr-") as tmp:
        input_path = Path(tmp) / "input.pdf"
        output_path = Path(tmp) / "archive.pdf"
        sidecar_path = Path(tmp) / "ocr.txt"
        if document.media_type == "application/pdf":
            input_path.write_bytes(source)
        else:
            try:
                if document.media_type in {"image/heic", "image/heif"}:
                    from pillow_heif import register_heif_opener

                    register_heif_opener()
                image = Image.open(io.BytesIO(source))
                frames = []
                index = 0
                while True:
                    frames.append(image.copy().convert("RGB"))
                    index += 1
                    try:
                        image.seek(index)
                    except EOFError:
                        break
                frames[0].save(
                    input_path,
                    "PDF",
                    save_all=True,
                    append_images=frames[1:],
                    resolution=300,
                )
            except Exception as exc:
                raise DocumentError(f"Could not convert this image: {exc}") from exc
        command = [
            "ocrmypdf",
            "--skip-text",
            "--rotate-pages",
            "--deskew",
            "--output-type",
            "pdfa",
            "--language",
            "eng+fra",
            "--sidecar",
            str(sidecar_path),
            str(input_path),
            str(output_path),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=240, check=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise DocumentError(f"OCR engine unavailable: {exc}") from exc
        if completed.returncode not in {0, 6}:  # 6 = already has text
            detail = (completed.stderr or completed.stdout).strip()[-1200:]
            raise DocumentError(f"OCR failed: {detail}")
        pdf = (
            output_path.read_bytes()
            if output_path.exists()
            else input_path.read_bytes()
        )
        text = sidecar_path.read_text(errors="replace") if sidecar_path.exists() else ""
        if not text.strip():
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(pdf))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return pdf, text


def _normalise_address(value: str) -> str:
    value = value.casefold()
    replacements = {
        "street": "st",
        "avenue": "ave",
        "road": "rd",
        "boulevard": "blvd",
        "drive": "dr",
        "apartment": "unit",
    }
    words = re.findall(r"[a-z0-9]+", value)
    return " ".join(replacements.get(word, word) for word in words)


def _holding_match(landlord, text: str):
    haystack = _normalise_address(text)
    matches = []
    for holding in PropertyHolding.objects.filter(landlord=landlord):
        address = _normalise_address(holding.address)
        name = _normalise_address(holding.name)
        score = 0
        if address and address in haystack:
            score = 1
        elif name and name in haystack:
            score = Decimal("0.85")
        if score:
            matches.append((holding, Decimal(score)))
    matches.sort(key=lambda row: row[1], reverse=True)
    if not matches:
        return None, Decimal("0")
    if len(matches) > 1 and matches[0][1] == matches[1][1]:
        return None, Decimal("0.40")
    return matches[0]


def resolve_holding_scope(landlord, scope_query: str) -> dict:
    """Resolve an address/holding without collapsing it to a child listing.

    When a physical holding has not been created yet, an exact shared listing
    address is enough to propose one. Listings with distinct legal addresses
    remain separate because only the normalized exact address joins them.
    """
    query = _normalise_address(scope_query or "")
    if not query:
        return {"error": "Give the physical property address or holding name."}

    holdings = []
    for holding in PropertyHolding.objects.filter(landlord=landlord):
        if query in {
            _normalise_address(holding.name),
            _normalise_address(holding.address),
        }:
            holdings.append(holding)
    if len(holdings) > 1:
        return {
            "error": "More than one physical holding matches that scope.",
            "options": [h.name for h in holdings],
        }
    if holdings:
        holding = holdings[0]
        return {
            "holding": holding,
            "create": False,
            "address": holding.address,
            "city": holding.city,
            "listings": list(
                holding.listings.order_by("name").values_list("name", flat=True)
            ),
            "matching_properties": list(holding.listings.all()),
        }

    from rentium.properties.models import Property

    properties = [
        prop
        for prop in Property.objects.filter(landlord=landlord).order_by("name")
        if _normalise_address(prop.address) == query
    ]
    if not properties:
        return {
            "error": (
                f"No physical holding or exact listing address matches "
                f"{scope_query!r}."
            )
        }
    addresses = {(p.address or "").strip() for p in properties}
    if len(addresses) != 1:
        return {
            "error": "That address is ambiguous.",
            "options": sorted(addresses),
        }
    address = addresses.pop()
    cities = sorted({(p.city or "").strip() for p in properties if p.city})
    return {
        "holding": None,
        "create": True,
        "address": address,
        "city": cities[0] if len(cities) == 1 else "",
        "listings": [p.name for p in properties],
        "matching_properties": properties,
    }


@transaction.atomic
def catalog_document_scope(
    landlord,
    *,
    document_id: str,
    scope_query: str,
    actor=None,
    issuer: str = "",
    document_date=None,
    confirm: bool = False,
) -> dict:
    """Attach a business record to a physical holding, never a forced listing."""
    document = RamaDocument.objects.filter(
        pk=document_id, landlord=landlord
    ).first()
    if document is None:
        return {"error": "No such business document in this portfolio."}
    resolved = resolve_holding_scope(landlord, scope_query)
    if resolved.get("error"):
        return resolved
    preview = {
        "document_id": str(document.pk),
        "document": document.title or document.original_filename,
        "scope": resolved["address"] or scope_query,
        "scope_kind": "physical_property_holding",
        "create_holding": resolved["create"],
        "child_listings": resolved["listings"],
        "issuer": issuer or document.issuer or None,
        "document_date": str(document_date or document.document_date or "") or None,
        "rule": (
            "The document belongs to the address-level holding. It is not attached "
            "to any individual room or unit."
        ),
    }
    if not confirm:
        return {
            "needs_confirm": True,
            "action": "catalog_business_document",
            "preview": preview,
            "instruction": (
                "Show this address-level filing preview. If approved, call "
                "catalog_business_document again with the same arguments and "
                "confirm=yes."
            ),
        }

    holding = resolved["holding"]
    if holding is None:
        holding, _ = PropertyHolding.objects.get_or_create(
            landlord=landlord,
            address=resolved["address"],
            defaults={
                "name": resolved["address"],
                "city": resolved["city"],
                "kind": PropertyHolding.Kind.HOUSE,
            },
        )
    assigned = []
    for prop in resolved["matching_properties"]:
        if prop.holding_id is None:
            prop.holding = holding
            prop.save(update_fields=["holding", "updated_at"])
            assigned.append(prop.name)

    document.holding = holding
    document.property = None
    document.portfolio_wide = False
    if issuer:
        document.issuer = issuer.strip()[:200]
    if document_date:
        document.document_date = document_date
    document.match_confidence = Decimal("1")
    document.extracted_data = {
        **(document.extracted_data or {}),
        "user_scope_locked": True,
        "scope_source": "landlord_address_confirmation",
    }
    document.clarification_answer = (
        f"Physical property scope confirmed as {holding.address or holding.name}."
    )
    document.canonical_filename = _canonical_name(document)
    _relocate_archive(document)
    document.full_clean()
    document.save()
    _event(
        document,
        RamaDocumentEvent.Kind.CLARIFIED,
        actor=actor,
        holding_id=str(holding.pk),
        address=holding.address,
        scope="physical_property_holding",
    )
    document = _ensure_ocr(document)
    intelligence = document_intelligence_payload(document)
    return {
        "updated": True,
        "catalogued": True,
        "document_id": str(document.pk),
        "documents_page": (
            url_for_path(f"/dashboard/documents?document={document.pk}")
        ),
        "holding": {
            "id": str(holding.pk),
            "name": holding.name,
            "address": holding.address,
        },
        "property": None,
        "child_listings": resolved["listings"],
        "listings_newly_linked_to_holding": assigned,
        "status": document.status,
        "note": (
            "Stored at the physical-property level. No individual room or unit "
            "was selected."
        ),
        # Chat must use these OCR facts — never invent amount/kind.
        "intelligence": intelligence,
        "relay_instruction": (
            "Relay the OCR intelligence to the landlord: kind, title, amount "
            "(verbatim), payment_state, and next_steps. If expense_like and "
            "payment_state is UNKNOWN, ASK whether it already left the bank. "
            "Then preview file_business_document — do not invent amounts."
        ),
    }


def _bytes_as_upload(filename: str, data: bytes, content_type: str = ""):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(
        filename or "document",
        data,
        content_type=(content_type or "application/octet-stream")[:160],
    )


def _duplicate_catalog_payload(document: RamaDocument, *, source: str) -> dict:
    """Hard stop when this exact file is already in the document inbox."""
    document = _ensure_ocr(document)
    intelligence = document_intelligence_payload(document)
    filed = document.status == RamaDocument.Status.FILED
    holding = None
    if document.holding_id:
        holding = {
            "id": str(document.holding_id),
            "name": document.holding.name,
            "address": document.holding.address,
        }
    return {
        "already_done": True,
        "is_duplicate": True,
        "duplicate_of_document_id": str(document.pk),
        "document_id": str(document.pk),
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "status": document.status,
        "holding": holding,
        "ledger_entry_id": (
            str(document.ledger_entry_id) if document.ledger_entry_id else None
        ),
        "expense_already_posted": bool(document.ledger_entry_id),
        "documents_page": url_for_path(
            f"/dashboard/documents?document={document.pk}"
        ),
        "intelligence": intelligence,
        "message": (
            f"This exact file is already catalogued as document {document.pk}"
            + (
                f" for {holding['address'] or holding['name']}."
                if holding
                else " (holding not set yet)."
            )
            + (
                " An expense is already linked on the ledger."
                if document.ledger_entry_id
                else (
                    " No ledger expense yet — use file_business_document if you "
                    "want to post one."
                    if intelligence.get("expense_like")
                    else ""
                )
            )
        ),
        "relay_instruction": (
            "Tell the landlord this is a DUPLICATE of an existing document "
            f"(id {document.pk}, stored "
            f"{document.created_at.isoformat() if document.created_at else 'earlier'}). "
            "Do NOT ask for the address again and do NOT re-catalog. "
            "If they want a ledger expense and none is linked, call "
            "file_business_document with this document_id."
        ),
        "source": source,
        "filed": filed,
    }


def promote_chat_file_to_document(
    landlord,
    *,
    attachment_id: str = "",
    upload_id: str = "",
    actor=None,
) -> dict:
    """Ingest chat file by content hash, OCR it, detect duplicates.

    Does NOT require a holding address. Safe to call on every attachment turn.
    """
    from .models import RamaAttachment, RamaUpload

    aid = (attachment_id or "").strip()
    uid = (upload_id or "").strip()
    if not aid and not uid:
        return {"error": "Pass attachment_id or upload_id."}

    staged_attachment = None
    staged_upload = None
    data = None
    filename = "document"
    content_type = ""

    if aid:
        staged_attachment = (
            RamaAttachment.objects.select_related("batch")
            .filter(pk=aid, batch__landlord=landlord)
            .first()
        )
        if staged_attachment is None:
            return {"error": f"No attachment {aid!r}."}
        # Already linked to a document from a prior prepare/catalog step.
        if staged_attachment.target_id:
            existing = RamaDocument.objects.filter(
                pk=staged_attachment.target_id, landlord=landlord
            ).first()
            if existing is not None:
                if (
                    existing.holding_id
                    or existing.status == RamaDocument.Status.FILED
                ):
                    return {
                        **_duplicate_catalog_payload(
                            existing, source="attachment_already_linked"
                        ),
                        "attachment_id": str(staged_attachment.pk),
                        "attachment_batch_id": str(staged_attachment.batch_id),
                    }
                # Unscoped prepare — return same document_id for the address step.
                document = _ensure_ocr(existing)
                intelligence = document_intelligence_payload(document)
                return {
                    "prepared": True,
                    "is_new_document": False,
                    "is_duplicate": False,
                    "document_id": str(document.pk),
                    "status": document.status,
                    "holding": None,
                    "needs_scope": True,
                    "intelligence": intelligence,
                    "attachment_id": str(staged_attachment.pk),
                    "attachment_batch_id": str(staged_attachment.batch_id),
                    "next_steps": [
                        f"catalog_business_document document_id={document.pk} "
                        "scope_query=<address> (attachment_id optional)."
                    ],
                    "relay_instruction": (
                        f"Document already prepared (document_id={document.pk}). "
                        "Relay OCR intelligence, ask for holding address if needed, "
                        "then catalog with document_id + scope_query — do NOT "
                        "require the old attachment_id."
                    ),
                }
        staged_attachment.original.open("rb")
        try:
            data = staged_attachment.original.read()
        finally:
            staged_attachment.original.close()
        filename = staged_attachment.original_filename or "attachment"
        content_type = staged_attachment.content_type or ""
    else:
        staged_upload = RamaUpload.objects.filter(pk=uid, landlord=landlord).first()
        if staged_upload is None:
            return {
                "error": (
                    f"No upload {uid!r}. If this file was already prepared, pass "
                    "document_id from the previous tool result (not upload_id)."
                ),
            }
        staged_upload.image.open("rb")
        try:
            data = staged_upload.image.read()
        finally:
            staged_upload.image.close()
        filename = Path(staged_upload.image.name).name
        suffix = Path(filename).suffix.lower()
        content_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
            ".webp": "image/webp",
            ".heic": "image/heic",
            ".heif": "image/heif",
            ".pdf": "application/pdf",
        }.get(suffix, "image/jpeg")

    digest = hashlib.sha256(data).hexdigest()
    existing = RamaDocument.objects.filter(landlord=landlord, sha256=digest).first()
    created = False
    if existing is not None:
        # Already fully catalogued (holding set or filed) → hard stop.
        if existing.holding_id or existing.status == RamaDocument.Status.FILED:
            if staged_attachment is not None and staged_attachment.status != (
                RamaAttachment.Status.APPLIED
            ):
                staged_attachment.classification = (
                    RamaAttachment.Classification.DOCUMENT
                )
                staged_attachment.status = RamaAttachment.Status.APPLIED
                staged_attachment.target_type = "rama_document"
                staged_attachment.target_id = str(existing.pk)
                staged_attachment.result = {
                    "document_id": str(existing.pk),
                    "created": False,
                    "duplicate": True,
                }
                staged_attachment.save(
                    update_fields=[
                        "classification",
                        "status",
                        "target_type",
                        "target_id",
                        "result",
                        "updated_at",
                    ]
                )
            if staged_upload is not None and staged_upload.used_at is None:
                staged_upload.used_at = timezone.now()
                staged_upload.save(update_fields=["used_at"])
            payload = _duplicate_catalog_payload(existing, source="sha256_match")
            if staged_attachment is not None:
                payload["attachment_id"] = str(staged_attachment.pk)
                payload["attachment_batch_id"] = str(staged_attachment.batch_id)
            if staged_upload is not None:
                payload["upload_id"] = str(staged_upload.pk)
            return payload
        # Same bytes already ingested but not yet scoped — continue with that row.
        document = existing
    else:
        upload = _bytes_as_upload(filename, data, content_type)
        document, created = ingest_document(
            landlord=landlord, upload=upload, created_by=actor
        )
    document = _ensure_ocr(document)

    # Link staged row → document but keep CLASSIFIED until holding is set so
    # a follow-up catalog with the same attachment_id (or document_id alone)
    # still works. APPLIED only after successful scope.
    if staged_attachment is not None:
        staged_attachment.classification = RamaAttachment.Classification.DOCUMENT
        staged_attachment.status = RamaAttachment.Status.CLASSIFIED
        staged_attachment.target_type = "rama_document"
        staged_attachment.target_id = str(document.pk)
        staged_attachment.result = {
            "document_id": str(document.pk),
            "created": created,
            "prepared": True,
        }
        staged_attachment.save(
            update_fields=[
                "classification",
                "status",
                "target_type",
                "target_id",
                "result",
                "updated_at",
            ]
        )

    intelligence = document_intelligence_payload(document)
    needs_scope = document.holding_id is None
    next_steps = []
    if needs_scope:
        next_steps.append(
            "Ask which physical property address this belongs to "
            "(or whole portfolio). Then call catalog_business_document with "
            f"document_id={document.pk} and scope_query=<address>."
        )
    else:
        next_steps.append(
            "Holding already set. If expense_like, ask paid/unpaid and call "
            f"file_business_document document_id={document.pk}."
        )

    return {
        "prepared": True,
        "is_new_document": created,
        "is_duplicate": False,
        "document_id": str(document.pk),
        "sha256": digest,
        "status": document.status,
        "holding": (
            {
                "id": str(document.holding_id),
                "name": document.holding.name,
                "address": document.holding.address,
            }
            if document.holding_id
            else None
        ),
        "needs_scope": needs_scope,
        "intelligence": intelligence,
        "attachment_id": str(staged_attachment.pk) if staged_attachment else None,
        "attachment_batch_id": (
            str(staged_attachment.batch_id) if staged_attachment else None
        ),
        "upload_id": str(staged_upload.pk) if staged_upload else None,
        "next_steps": next_steps,
        "relay_instruction": (
            "FIRST relay OCR intelligence (kind, title, amount — never invent). "
            + (
                "Document is prepared. Ask once for the holding address, then "
                f"call catalog_business_document with document_id={document.pk} "
                "and scope_query (attachment_id optional)."
                if needs_scope
                else "Holding is known. Proceed to file_business_document if "
                "they want a ledger expense."
            )
        ),
    }


@transaction.atomic
def catalog_staged_photo_as_document(
    landlord,
    *,
    upload_id: str,
    scope_query: str = "",
    actor=None,
    issuer: str = "",
    document_date=None,
    confirm: bool = False,
) -> dict:
    """Promote a chat photo of mail/receipt into the document pipeline.

    Inspect/OCR/hash first. If the same file was already catalogued, return
    already_done. Scope is only required for new (or unscoped) documents.
    """
    prepared = promote_chat_file_to_document(
        landlord, upload_id=upload_id, actor=actor
    )
    if prepared.get("error"):
        return prepared
    if prepared.get("already_done") or prepared.get("is_duplicate"):
        return prepared

    document_id = prepared["document_id"]
    document = RamaDocument.objects.filter(pk=document_id, landlord=landlord).first()
    if document is None:
        return {"error": "Document disappeared after prepare."}

    # Scope already known from prior catalog / OCR match.
    if document.holding_id and not (scope_query or "").strip():
        from .models import RamaUpload

        staged = RamaUpload.objects.filter(pk=upload_id, landlord=landlord).first()
        if staged and staged.used_at is None:
            staged.used_at = timezone.now()
            staged.save(update_fields=["used_at"])
        return {
            "already_done": True,
            "document_id": str(document.pk),
            "message": (
                f"Document already filed against "
                f"{document.holding.address or document.holding.name}."
            ),
            "intelligence": document_intelligence_payload(document),
            "holding": {
                "id": str(document.holding_id),
                "name": document.holding.name,
                "address": document.holding.address,
            },
            "relay_instruction": (
                "Already catalogued. Do not ask for address again. "
                "Offer file_business_document if they want a ledger expense."
            ),
        }

    if not (scope_query or "").strip():
        return {
            "needs_input": True,
            "document_id": str(document.pk),
            "question_for_user": (
                "Which physical property address does this belong to? "
                "(You can also say the whole portfolio.)"
            ),
            "intelligence": prepared.get("intelligence"),
            "relay_instruction": (
                "Show OCR findings FIRST (kind, amount). Then ask "
                "question_for_user. When they answer, call "
                f"catalog_business_document with document_id={document.pk} "
                "and scope_query=<their address>."
            ),
            "upload_id": upload_id,
        }

    resolved = resolve_holding_scope(landlord, scope_query)
    if resolved.get("error"):
        return resolved
    if not confirm:
        return {
            "needs_confirm": True,
            "action": "catalog_business_document",
            "preview": {
                "document_id": str(document.pk),
                "upload_id": upload_id,
                "document": document.title or document.original_filename,
                "scope": resolved["address"] or scope_query,
                "scope_kind": "physical_property_holding",
                "create_holding": resolved["create"],
                "child_listings": resolved["listings"],
                "is_duplicate": False,
                "intelligence": document_intelligence_payload(document),
                "issuer": issuer or None,
                "document_date": str(document_date or "") or None,
                "rule": (
                    "New document (content hash not seen before). File at the "
                    "holding — not a room listing."
                ),
            },
            "instruction": (
                "Show OCR intelligence + this filing preview. On yes, call "
                "catalog_business_document again with the same document_id/"
                "scope_query/upload_id and confirm=yes."
            ),
        }

    result = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query=scope_query,
        actor=actor,
        issuer=issuer,
        document_date=document_date,
        confirm=True,
    )
    if result.get("error"):
        return result
    from .models import RamaUpload

    staged = RamaUpload.objects.filter(pk=upload_id, landlord=landlord).first()
    if staged and staged.used_at is None:
        staged.used_at = timezone.now()
        staged.save(update_fields=["used_at"])
    return {
        **result,
        "promoted_from_upload_id": upload_id,
        "ocr_complete": True,
    }


@transaction.atomic
def catalog_batch_attachment_as_document(
    landlord,
    *,
    attachment_id: str,
    scope_query: str = "",
    actor=None,
    issuer: str = "",
    document_date=None,
    confirm: bool = False,
) -> dict:
    """Promote one attachment-batch item: hash/OCR first, then scope.

    Same-file re-sends return already_done with the existing document — never
    a fresh "store for 950 McKenzie" preview that looks like a new filing.
    """
    prepared = promote_chat_file_to_document(
        landlord, attachment_id=attachment_id, actor=actor
    )
    if prepared.get("error"):
        return prepared
    if prepared.get("already_done") or prepared.get("is_duplicate"):
        return prepared

    document_id = prepared["document_id"]
    document = (
        RamaDocument.objects.select_related("holding")
        .filter(pk=document_id, landlord=landlord)
        .first()
    )
    if document is None:
        return {"error": "Document disappeared after prepare."}

    if document.holding_id and not (scope_query or "").strip():
        return {
            "already_done": True,
            "document_id": str(document.pk),
            "message": (
                f"Document already catalogued for "
                f"{document.holding.address or document.holding.name}."
            ),
            "intelligence": document_intelligence_payload(document),
            "holding": {
                "id": str(document.holding_id),
                "name": document.holding.name,
                "address": document.holding.address,
            },
            "attachment_id": attachment_id,
            "relay_instruction": (
                "Already catalogued — do not re-file. Use file_business_document "
                "only if they want a ledger expense and none is linked."
            ),
        }

    if not (scope_query or "").strip():
        return {
            "needs_input": True,
            "document_id": str(document.pk),
            "attachment_id": attachment_id,
            "question_for_user": (
                "Which physical property address does this belong to? "
                "(You can also say the whole portfolio.)"
            ),
            "intelligence": prepared.get("intelligence"),
            "relay_instruction": (
                "Show OCR findings FIRST (kind, amount from intelligence — "
                "never invent). Then ask question_for_user. When they answer, "
                f"call catalog_business_document with document_id={document.pk} "
                "and scope_query=<address> (attachment_id optional)."
            ),
        }

    resolved = resolve_holding_scope(landlord, scope_query)
    if resolved.get("error"):
        return resolved
    if not confirm:
        return {
            "needs_confirm": True,
            "action": "catalog_business_document",
            "preview": {
                "document_id": str(document.pk),
                "attachment_id": attachment_id,
                "attachment_batch_id": prepared.get("attachment_batch_id"),
                "document": document.title or document.original_filename,
                "scope": resolved["address"] or scope_query,
                "scope_kind": "physical_property_holding",
                "convert_to_ocr_document": True,
                "create_holding": resolved["create"],
                "child_listings": resolved["listings"],
                "is_duplicate": False,
                "is_new_document": prepared.get("is_new_document"),
                "intelligence": document_intelligence_payload(document),
                "issuer": issuer or None,
                "document_date": str(document_date or "") or None,
            },
            "instruction": (
                "Show OCR intelligence + filing preview. On yes: "
                "catalog_business_document with same document_id/scope_query "
                "and confirm=yes."
            ),
        }

    result = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query=scope_query,
        actor=actor,
        issuer=issuer,
        document_date=document_date,
        confirm=True,
    )
    if result.get("error"):
        return result
    # Scope complete — mark attachment APPLIED if we still have a handle.
    from .models import RamaAttachment

    att = RamaAttachment.objects.filter(
        pk=attachment_id, batch__landlord=landlord
    ).first()
    if att is not None:
        att.status = RamaAttachment.Status.APPLIED
        att.target_type = "rama_document"
        att.target_id = str(document.pk)
        att.classification = RamaAttachment.Classification.DOCUMENT
        att.save(
            update_fields=[
                "status",
                "target_type",
                "target_id",
                "classification",
                "updated_at",
            ]
        )
    intelligence = result.get("intelligence") or {}
    return {
        **result,
        "promoted_from_attachment_id": attachment_id,
        "attachment_batch_id": prepared.get("attachment_batch_id"),
        "ocr_complete": bool(
            intelligence.get("ocr_excerpt") or intelligence.get("amount")
        ),
    }


def document_location(landlord, document_id: str) -> dict:
    """Describe both the logical archive key and its physical storage backend."""
    document = RamaDocument.objects.select_related("holding").filter(
        pk=document_id,
        landlord=landlord,
    ).first()
    if document is None:
        return {"error": "No such business document in this portfolio."}
    field = document.archival_pdf or document.original_file
    storage = field.storage
    storage_key = field.name
    backend = f"{storage.__class__.__module__}.{storage.__class__.__name__}"
    container_path = None
    try:
        container_path = storage.path(storage_key)
    except (AttributeError, NotImplementedError):
        pass

    location_prefix = str(getattr(storage, "location", "") or "").strip("/")
    bucket = str(getattr(storage, "bucket_name", "") or "")
    object_key = "/".join(part for part in (location_prefix, storage_key) if part)
    if bucket:
        manual_location = f"s3://{bucket}/{object_key}"
        storage_kind = "object_storage"
    elif container_path:
        manual_location = container_path
        storage_kind = "container_filesystem"
    else:
        manual_location = storage_key
        storage_kind = "storage_backend_key"

    return {
        "document_id": str(document.pk),
        "title": document.title or document.original_filename,
        "status": document.status,
        "holding": document.holding.name if document.holding_id else None,
        "canonical_filename": document.canonical_filename or None,
        "file_variant": "archival_pdfa" if document.archival_pdf else "original",
        "storage_kind": storage_kind,
        "storage_backend": backend,
        "storage_key": storage_key,
        "manual_location": manual_location,
        "container_path": container_path,
        "object_bucket": bucket or None,
        "object_key": object_key if bucket else None,
        "documents_page": (
            url_for_path(f"/dashboard/documents?document={document.pk}")
        ),
        "authenticated_download_path": (
            f"/api/rama/documents/{document.pk}/download/"
        ),
        "explanation": (
            "container_path is present only for filesystem storage. Production "
            "uses object storage, so its durable manual location is the s3:// "
            "bucket/key rather than a path inside the Django container."
        ),
    }


def _parse_money(raw: str) -> Decimal | None:
    try:
        value = Decimal(str(raw).replace(",", "").strip())
        if value > 0:
            return value
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None


def _all_money_values(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for raw in re.findall(r"(?:CAD\s*)?\$\s*([\d,]+(?:\.\d{2})?)", text or "", re.I):
        value = _parse_money(raw)
        if value is not None:
            values.append(value)
    return values


def _first_money(text: str) -> Decimal | None:
    """Legacy helper: first positive money figure in reading order."""
    values = _all_money_values(text)
    return values[0] if values else None


def _extract_amount(text: str) -> Decimal | None:
    """Best-effort invoice/receipt total — not the first random $ figure.

    Prefer labeled totals (Total / Amount Due / Balance Due / Grand Total).
    When several match, use the last (usually grand total). Otherwise the
    largest money figure on the page (subtotals are usually smaller).
    """
    labeled = re.findall(
        r"(?:grand\s*total|invoice\s*total|amount\s*due|balance\s*due|"
        r"total\s*due|total\s*amount|amount\s*paid|total)\b"
        r"[^\n\d$]{0,40}\$?\s*([\d,]+(?:\.\d{2})?)",
        text or "",
        re.I,
    )
    for raw in reversed(labeled):
        value = _parse_money(raw)
        if value is not None:
            return value
    values = _all_money_values(text)
    return max(values) if values else None


def _classify(text: str, filename: str) -> dict:
    value = f"{filename}\n{text}".casefold()
    rules = [
        # First, because a statement contains most of the words below — the
        # word "invoice" on page 3 of a bank statement must not make it one.
        (
            (
                "account statement", "bank statement", "statement of account",
                "opening balance", "closing balance", "transaction history",
                "credit card statement",
            ),
            RamaDocument.Kind.BANK_STATEMENT,
            "",
        ),
        (
            ("property tax", "tax notice", "property assessment"),
            RamaDocument.Kind.TAX,
            ExpenseCategory.PROPERTY_TAX,
        ),
        (
            ("mortgage", "renewal", "amortization"),
            RamaDocument.Kind.MORTGAGE,
            ExpenseCategory.MORTGAGE,
        ),
        (
            ("insurance", "policy premium"),
            RamaDocument.Kind.INSURANCE,
            ExpenseCategory.INSURANCE,
        ),
        # Maintenance BEFORE generic invoice — "Invoice for window screens"
        # is a maintenance expense, not a vague "Expense Document".
        (
            (
                "repair",
                "work order",
                "plumbing",
                "electrical",
                "maintenance",
                "window screen",
                "window screens",
                "screens",
                "hvac",
                "furnace",
                "appliance",
                "handyman",
                "contractor",
            ),
            RamaDocument.Kind.MAINTENANCE,
            ExpenseCategory.MAINTENANCE,
        ),
        (
            ("invoice", "receipt", "amount due", "subtotal"),
            RamaDocument.Kind.EXPENSE,
            ExpenseCategory.OTHER,
        ),
        (("lease", "tenancy agreement"), RamaDocument.Kind.LEASE, ""),
    ]
    kind, category, confidence = RamaDocument.Kind.OTHER, "", Decimal("0.45")
    for terms, candidate_kind, candidate_category in rules:
        hits = sum(term in value for term in terms)
        if hits:
            kind, category = candidate_kind, candidate_category
            confidence = min(Decimal("0.70") + Decimal("0.08") * hits, Decimal("0.98"))
            break

    expense_like = kind in {
        RamaDocument.Kind.EXPENSE,
        RamaDocument.Kind.TAX,
        RamaDocument.Kind.MORTGAGE,
        RamaDocument.Kind.INSURANCE,
        RamaDocument.Kind.MAINTENANCE,
    }
    if expense_like:
        # Paid signals win over "invoice" alone — many paid invoices still
        # say "invoice" at the top.
        if any(
            term in value
            for term in (
                "paid in full",
                "payment received",
                "amount paid",
                "balance $0",
                "balance 0.00",
                "thank you for your payment",
                "payment processed",
            )
        ):
            payment_state = RamaDocument.PaymentState.PAID
        elif any(
            term in value
            for term in ("amount due", "due date", "balance due", "please pay")
        ):
            payment_state = RamaDocument.PaymentState.UNPAID
        elif "invoice" in value and "receipt" not in value:
            payment_state = RamaDocument.PaymentState.UNKNOWN
        elif "receipt" in value:
            # Receipt alone is weak evidence of paid — still ask if unclear.
            payment_state = RamaDocument.PaymentState.UNKNOWN
        else:
            payment_state = RamaDocument.PaymentState.UNKNOWN
    else:
        payment_state = RamaDocument.PaymentState.NOT_APPLICABLE

    # Prefer a short title from maintenance content when we can.
    title = {
        RamaDocument.Kind.TAX: "Property Tax Notice",
        RamaDocument.Kind.MORTGAGE: "Mortgage Document",
        RamaDocument.Kind.INSURANCE: "Insurance Document",
        RamaDocument.Kind.EXPENSE: "Expense Invoice",
        RamaDocument.Kind.MAINTENANCE: "Maintenance Invoice",
        RamaDocument.Kind.LEASE: "Lease Document",
        RamaDocument.Kind.BANK_STATEMENT: "Bank Statement",
    }.get(kind, "Business Document")
    if kind == RamaDocument.Kind.MAINTENANCE:
        for phrase in (
            "window screens",
            "window screen",
            "plumbing",
            "electrical",
            "hvac",
            "furnace",
        ):
            if phrase in value:
                title = f"Maintenance — {phrase.title()}"
                break

    return {
        "kind": kind,
        "category": category,
        "confidence": confidence,
        "payment_state": payment_state,
        "title": title,
        # A statement's first money figure is an opening balance, not a cost.
        # Showing it as the document's amount invites someone to file it.
        "amount": (
            None
            if kind == RamaDocument.Kind.BANK_STATEMENT
            else _extract_amount(text)
        ),
    }


def _canonical_name(document: RamaDocument) -> str:
    when = document.document_date or document.created_at.date()
    scope = document.holding.name if document.holding else "Portfolio"
    title = document.title or document.get_kind_display()
    ref = f"-{slugify(document.reference_number)}" if document.reference_number else ""
    return f"{when:%Y-%m-%d}_{slugify(scope)}_{slugify(title)}{ref}_{str(document.pk)[:8]}.pdf"


def _archive_path(document: RamaDocument) -> str:
    year = (document.document_date or document.created_at.date()).year
    holding = slugify(document.holding.name) if document.holding else "portfolio"
    category = slugify(document.get_kind_display())
    return (
        f"business_documents/{document.landlord_id}/{holding}/"
        f"{year}/{category}/{document.canonical_filename}"
    )


def _relocate_archive(document: RamaDocument) -> None:
    """Copy the PDF to its reviewed canonical path, then remove the old key."""
    if not document.archival_pdf:
        return
    target = _archive_path(document)
    if document.archival_pdf.name == target:
        return
    old_name = document.archival_pdf.name
    document.archival_pdf.open("rb")
    try:
        content = ContentFile(document.archival_pdf.read())
    finally:
        document.archival_pdf.close()
    document.archival_pdf.storage.save(target, content)
    document.archival_pdf.name = target
    document.archival_pdf.storage.delete(old_name)


def document_intelligence_payload(document: RamaDocument) -> dict:
    """What chat/UI must relay after OCR — never invent these numbers."""
    expense_like = document.kind in {
        RamaDocument.Kind.EXPENSE,
        RamaDocument.Kind.TAX,
        RamaDocument.Kind.MORTGAGE,
        RamaDocument.Kind.INSURANCE,
        RamaDocument.Kind.MAINTENANCE,
    }
    ocr_excerpt = (document.ocr_text or "").strip()
    if len(ocr_excerpt) > 400:
        ocr_excerpt = ocr_excerpt[:400] + "…"
    next_steps: list[str] = []
    if expense_like and document.amount:
        if document.payment_state == RamaDocument.PaymentState.UNKNOWN:
            next_steps.append(
                "ASK the landlord: has this amount already left the bank "
                "(paid) or is it still unpaid? Paid ≠ void — paid means "
                "post the expense with paid_on set."
            )
        next_steps.append(
            "Call file_business_document with this document_id, "
            f"amount={document.amount} (do NOT invent a different amount), "
            "payment_state=PAID or UNPAID from their answer, and confirm=yes "
            "only after they approve the preview."
        )
    elif expense_like and not document.amount:
        next_steps.append(
            "OCR did not extract an amount — ask the landlord for the total "
            "before filing, then pass it as amount= on file_business_document."
        )
    elif document.status == RamaDocument.Status.FAILED:
        next_steps.append(
            f"OCR failed: {document.failure_reason or 'unknown'}. "
            "Ask the landlord to re-send a clearer photo/PDF."
        )
    return {
        "document_id": str(document.pk),
        "status": document.status,
        "kind": document.kind,
        "kind_display": document.get_kind_display(),
        "title": document.title or document.original_filename,
        "amount": str(document.amount) if document.amount is not None else None,
        "currency": getattr(document, "currency", None) or "CAD",
        "expense_category": document.expense_category or None,
        "payment_state": document.payment_state,
        "issuer": document.issuer or None,
        "document_date": (
            str(document.document_date) if document.document_date else None
        ),
        "holding": (
            {
                "id": str(document.holding_id),
                "name": document.holding.name,
                "address": document.holding.address,
            }
            if document.holding_id
            else None
        ),
        "clarification_question": document.clarification_question or None,
        "ocr_excerpt": ocr_excerpt or None,
        "expense_like": expense_like,
        "ledger_entry_id": (
            str(document.ledger_entry_id) if document.ledger_entry_id else None
        ),
        "documents_page": url_for_path(
            f"/dashboard/documents?document={document.pk}"
        ),
        "next_steps": next_steps,
        "rules_for_model": (
            "NEVER invent or guess the amount — use amount from this payload "
            "only. NEVER offer to void an expense because it was paid. "
            "Paid means post with payment_state=PAID (sets paid_on). "
            "Unpaid means post with payment_state=UNPAID (paid_on null)."
        ),
    }


def _ensure_ocr(document: RamaDocument) -> RamaDocument:
    """Run OCR now if still pending (chat path needs facts before the reply)."""
    document.refresh_from_db()
    if document.status == RamaDocument.Status.FILED:
        return document
    if document.status == RamaDocument.Status.FAILED and (document.ocr_text or ""):
        return document
    if document.status in {
        RamaDocument.Status.READY,
        RamaDocument.Status.NEEDS_REVIEW,
    } and (document.ocr_text or "").strip():
        return document
    return process_document(document.pk)


def process_document(document_id) -> RamaDocument:
    """OCR and propose filing metadata. Never posts money without review."""
    document = RamaDocument.objects.select_related("landlord", "holding").get(
        pk=document_id
    )
    document.status = RamaDocument.Status.PROCESSING
    document.failure_reason = ""
    document.save(update_fields=["status", "failure_reason", "updated_at"])
    try:
        pdf, text = _pdf_and_text(document)
        result = _classify(text, document.original_filename)
        locked_scope = bool((document.extracted_data or {}).get("user_scope_locked"))
        if locked_scope and document.holding_id:
            holding, match_confidence = document.holding, Decimal("1")
        else:
            holding, match_confidence = _holding_match(document.landlord, text)
        document.ocr_text = text
        document.kind = result["kind"]
        document.expense_category = result["category"]
        document.payment_state = result["payment_state"]
        document.title = result["title"]
        document.amount = result["amount"]
        if holding is not None:
            document.holding = holding
        document.classification_confidence = result["confidence"]
        document.match_confidence = match_confidence
        reasons = []
        if not text.strip():
            reasons.append("I could not read enough text from the file.")
        if not document.holding_id:
            reasons.append("I could not confidently identify the property address.")
        if result["confidence"] < Decimal("0.70"):
            reasons.append("I could not confidently identify the document type.")
        if (
            result["payment_state"] == RamaDocument.PaymentState.UNKNOWN
            and result["amount"]
        ):
            reasons.append(
                "I cannot tell whether this amount has left the bank — "
                "ask if it is already paid."
            )
        document.clarification_question = " ".join(reasons)
        document.status = (
            RamaDocument.Status.NEEDS_REVIEW if reasons else RamaDocument.Status.READY
        )
        document.canonical_filename = _canonical_name(document)
        document.archival_pdf.save(
            _archive_path(document), ContentFile(pdf), save=False
        )
        document.extracted_data = {
            **(document.extracted_data or {}),
            "classifier": "rentium-rules-v2",
            "ocr_characters": len(text),
            "amount_extractor": "labeled_total_or_max",
        }
        document.full_clean()
        document.save()
        _event(
            document,
            RamaDocumentEvent.Kind.OCR_COMPLETED,
            characters=len(text),
        )
        _event(
            document,
            RamaDocumentEvent.Kind.CLASSIFIED,
            document_kind=document.kind,
            holding_id=str(document.holding_id) if document.holding_id else None,
            confidence=str(document.classification_confidence),
            amount=str(document.amount) if document.amount is not None else None,
        )
    except Exception as exc:
        document.status = RamaDocument.Status.FAILED
        document.failure_reason = str(exc)[:2000]
        document.save(update_fields=["status", "failure_reason", "updated_at"])
        _event(document, RamaDocumentEvent.Kind.FAILED, reason=str(exc)[:2000])
    return document


@transaction.atomic
def file_document(
    document: RamaDocument,
    *,
    actor,
    holding=None,
    property=None,
    kind=None,
    title=None,
    amount=None,
    expense_category=None,
    payment_state=None,
    document_date=None,
    due_date=None,
    issuer=None,
    reference_number=None,
    clarification_answer="",
    portfolio_wide=False,
    duplicate_resolution="",
) -> RamaDocument:
    """Apply human review, file the record, and post an expense if applicable.

    `duplicate_resolution` answers "have I already recorded this cost?":

    - ""              decide for me — refuse with candidates if any look like
                      this same cost (the default, so nothing doubles silently)
    - "new"           post it anyway; it is a genuinely separate cost
    - "link:<id>"     this receipt documents an expense already on the books —
                      attach the file to that entry and post nothing

    The sha256 check on upload only catches the same FILE twice. It cannot see
    that a cost was already entered by message and is now arriving as a photo,
    which is the way these actually double up.
    """
    if document.status == RamaDocument.Status.FILED:
        return document
    document.portfolio_wide = bool(portfolio_wide)
    document.holding = (
        None if document.portfolio_wide else (holding or document.holding)
    )
    document.property = (
        None if document.portfolio_wide else (property or document.property)
    )
    document.kind = kind or document.kind
    document.title = title or document.title
    document.amount = (
        Decimal(str(amount)) if amount not in (None, "") else document.amount
    )
    document.expense_category = expense_category or document.expense_category
    document.payment_state = payment_state or document.payment_state
    document.document_date = document_date or document.document_date
    document.due_date = due_date or document.due_date
    document.issuer = issuer if issuer is not None else document.issuer
    document.reference_number = (
        reference_number if reference_number is not None else document.reference_number
    )
    document.clarification_answer = clarification_answer

    if not document.portfolio_wide and not document.holding and not document.property:
        raise DocumentError("Choose the property holding, or mark it portfolio-wide.")
    if document.property and not document.holding:
        document.holding = document.property.holding
    if document.amount and document.kind in {
        RamaDocument.Kind.EXPENSE,
        RamaDocument.Kind.TAX,
        RamaDocument.Kind.MORTGAGE,
        RamaDocument.Kind.INSURANCE,
        RamaDocument.Kind.MAINTENANCE,
    }:
        if not document.expense_category:
            raise DocumentError("Choose an expense category.")
        if document.payment_state == RamaDocument.PaymentState.UNKNOWN:
            raise DocumentError("Confirm whether this expense has left the bank.")

        incurred = document.document_date or date.today()
        if duplicate_resolution.startswith("link:"):
            entry = _link_to_existing_expense(
                document, duplicate_resolution.split(":", 1)[1].strip(), actor
            )
        else:
            if not duplicate_resolution:
                candidates = ledger_services.find_duplicate_expense_candidates(
                    document.landlord,
                    amount=document.amount,
                    on_date=incurred,
                    property=document.property,
                    holding=document.holding,
                )
                if candidates:
                    raise DuplicateExpenseError(
                        "This may already be recorded. "
                        + "; ".join(
                            f"{c['description']} (${c['amount']}, "
                            f"{c['effective_date']}, {c['scope']})"
                            for c in candidates
                        )
                        + ". Attach this receipt to that entry, or confirm it "
                        "is a separate cost.",
                        candidates=candidates,
                    )
            entry, _ = post_expense(
                landlord=document.landlord,
                property=document.property,
                holding=document.holding,
                amount=document.amount,
                category=document.expense_category,
                description=document.title,
                incurred_date=incurred,
                vendor=document.issuer,
                paid_on=(
                    incurred
                    if document.payment_state == RamaDocument.PaymentState.PAID
                    else None
                ),
                idempotency_key=f"rama-document:{document.pk}",
                created_by=actor,
                metadata={
                    "source_document_id": str(document.pk),
                    "holding_id": str(document.holding_id)
                    if document.holding_id
                    else None,
                    "due_date": str(document.due_date) if document.due_date else None,
                },
            )
        document.ledger_entry = entry
        _event(
            document,
            RamaDocumentEvent.Kind.EXPENSE_POSTED,
            actor=actor,
            ledger_entry_id=str(entry.pk),
        )
    document.status = RamaDocument.Status.FILED
    document.filed_at = timezone.now()
    document.clarification_question = ""
    document.canonical_filename = _canonical_name(document)
    _relocate_archive(document)
    document.full_clean()
    document.save()
    if clarification_answer:
        _event(
            document,
            RamaDocumentEvent.Kind.CLARIFIED,
            actor=actor,
            answer=clarification_answer,
        )
    _event(document, RamaDocumentEvent.Kind.FILED, actor=actor)
    return document


def file_business_document_for_chat(
    landlord,
    *,
    document_id: str,
    payment_state: str = "",
    amount: str = "",
    title: str = "",
    expense_category: str = "",
    issuer: str = "",
    document_date: str = "",
    duplicate_resolution: str = "",
    confirm: str = "",
) -> dict:
    """Chat wrapper around file_document: post expense from OCR with paid/unpaid."""

    def _confirmed(value: str) -> bool:
        return str(value or "").strip().lower() in ("yes", "true", "1", "y", "confirm")

    def _preview(action: str, preview: dict, how: str) -> dict:
        return {
            "needs_confirm": True,
            "action": action,
            "preview": preview,
            "instruction": (
                f"Show this preview to the landlord. If they approve, call {action} "
                f"again with the same arguments AND confirm=yes. {how}"
            ),
            "ui_rules": True,
        }

    document = (
        RamaDocument.objects.select_related("holding", "ledger_entry")
        .filter(pk=document_id, landlord=landlord)
        .first()
    )
    if document is None:
        return {"error": f"No business document {document_id!r}."}
    if document.status == RamaDocument.Status.FILED:
        return {
            "already_done": True,
            "document_id": str(document.pk),
            "ledger_entry_id": (
                str(document.ledger_entry_id) if document.ledger_entry_id else None
            ),
            "message": "This document is already filed.",
            "intelligence": document_intelligence_payload(document),
        }

    document = _ensure_ocr(document)
    pay = (payment_state or "").strip().upper()
    if pay in ("PAID", "YES", "Y", "TRUE", "1"):
        pay = RamaDocument.PaymentState.PAID
    elif pay in ("UNPAID", "NO", "N", "FALSE", "0"):
        pay = RamaDocument.PaymentState.UNPAID
    elif pay:
        return {
            "error": "payment_state must be PAID or UNPAID.",
        }
    else:
        pay = document.payment_state
        if pay == RamaDocument.PaymentState.UNKNOWN:
            return {
                "needs_input": True,
                "question_for_user": (
                    f"Has ${document.amount} for "
                    f"{document.title or 'this invoice'} already left your bank "
                    f"(paid), or is it still unpaid?"
                ),
                "relay_instruction": (
                    "Ask question_for_user VERBATIM, then STOP. When they answer, "
                    "call file_business_document again with payment_state=PAID or "
                    "UNPAID. Paid means set paid_on — never void."
                ),
                "intelligence": document_intelligence_payload(document),
            }

    amt = amount.strip() if amount else ""
    if not amt and document.amount is not None:
        amt = str(document.amount)
    if not amt:
        return {
            "needs_input": True,
            "question_for_user": "What is the total amount on this invoice?",
            "relay_instruction": (
                "Ask for the amount, then call file_business_document with amount=."
            ),
            "intelligence": document_intelligence_payload(document),
        }

    category = (expense_category or document.expense_category or "").strip()
    if not category:
        if document.kind == RamaDocument.Kind.MAINTENANCE:
            category = ExpenseCategory.MAINTENANCE
        else:
            category = ExpenseCategory.OTHER
    kind = document.kind
    if kind not in {
        RamaDocument.Kind.EXPENSE,
        RamaDocument.Kind.TAX,
        RamaDocument.Kind.MORTGAGE,
        RamaDocument.Kind.INSURANCE,
        RamaDocument.Kind.MAINTENANCE,
    }:
        kind = (
            RamaDocument.Kind.MAINTENANCE
            if category == ExpenseCategory.MAINTENANCE
            else RamaDocument.Kind.EXPENSE
        )

    desc = (title or document.title or "Business expense").strip()[:255]
    vend = (issuer if issuer != "" else document.issuer) or ""
    ddate = None
    if (document_date or "").strip():
        try:
            ddate = date.fromisoformat(document_date.strip()[:10])
        except ValueError:
            return {"error": "document_date must be YYYY-MM-DD."}
    else:
        ddate = document.document_date

    preview = {
        "document_id": str(document.pk),
        "amount": amt,
        "title": desc,
        "expense_category": category,
        "kind": kind,
        "payment_state": pay,
        "paid_means": (
            "Expense posts with paid_on set (already left the bank)."
            if pay == RamaDocument.PaymentState.PAID
            else "Expense posts unpaid (not yet taken from bank)."
        ),
        "holding": (
            document.holding.address or document.holding.name
            if document.holding_id
            else None
        ),
        "issuer": vend or None,
        "document_date": str(ddate) if ddate else None,
        "side_effects": [
            "File the document",
            "Post one ledger EXPENSE via post_expense (append-only)",
            "Link the document to that expense",
        ],
        "never": "Never void because the expense was paid.",
    }
    if not _confirmed(confirm):
        return _preview(
            "file_business_document",
            preview,
            "Files the document and posts the expense to the ledger.",
        )

    try:
        filed = file_document(
            document,
            actor=getattr(landlord, "user", None),
            holding=document.holding,
            kind=kind,
            title=desc,
            amount=amt,
            expense_category=category,
            payment_state=pay,
            document_date=ddate,
            issuer=vend,
            duplicate_resolution=(duplicate_resolution or "").strip(),
        )
    except DuplicateExpenseError as exc:
        return {
            "error": str(exc),
            "code": "DUPLICATE_EXPENSE",
            "candidates": exc.candidates,
            "resolutions": {
                "link": "duplicate_resolution=link:<entry_id>",
                "separate": "duplicate_resolution=new",
            },
        }
    except DocumentError as exc:
        return {"error": str(exc)}

    entry = filed.ledger_entry
    return {
        "filed": True,
        "document_id": str(filed.pk),
        "ledger_entry_id": str(entry.pk) if entry else None,
        "amount": str(entry.amount) if entry else amt,
        "paid_on": str(entry.paid_on) if entry and entry.paid_on else None,
        "holding": (
            filed.holding.address or filed.holding.name if filed.holding_id else None
        ),
        "documents_page": url_for_path(f"/dashboard/documents?document={filed.pk}"),
        "message": (
            f"Filed and posted expense ${amt}"
            + (
                f" (paid {entry.paid_on})."
                if entry and entry.paid_on
                else " (not yet taken from bank)."
            )
        ),
        "intelligence": document_intelligence_payload(filed),
    }


def business_document_status(landlord, *, document_id: str = "") -> dict:
    """Read OCR/classification state for a business document."""
    document = (
        RamaDocument.objects.select_related("holding", "ledger_entry")
        .filter(pk=document_id, landlord=landlord)
        .first()
    )
    if document is None:
        return {"error": f"No business document {document_id!r}."}
    if not (document.ocr_text or "").strip() and document.status not in {
        RamaDocument.Status.FILED,
        RamaDocument.Status.FAILED,
    }:
        document = _ensure_ocr(document)
    return {"ok": True, **document_intelligence_payload(document)}
