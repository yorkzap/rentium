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
    }


@transaction.atomic
def catalog_staged_photo_as_document(
    landlord,
    *,
    upload_id: str,
    scope_query: str,
    actor=None,
    issuer: str = "",
    document_date=None,
    confirm: bool = False,
) -> dict:
    """Promote a chat photo of mail/receipt into the document pipeline."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from .models import RamaUpload

    staged = RamaUpload.objects.filter(
        pk=upload_id,
        landlord=landlord,
        used_at__isnull=True,
    ).first()
    if staged is None:
        return {"error": "No unused attached photo with that upload_id."}
    resolved = resolve_holding_scope(landlord, scope_query)
    if resolved.get("error"):
        return resolved
    if not confirm:
        return {
            "needs_confirm": True,
            "action": "catalog_business_document",
            "preview": {
                "upload_id": str(staged.pk),
                "document": Path(staged.image.name).name,
                "scope": resolved["address"] or scope_query,
                "scope_kind": "physical_property_holding",
                "convert_photo_to_ocr_document": True,
                "create_holding": resolved["create"],
                "child_listings": resolved["listings"],
                "issuer": issuer or None,
                "document_date": str(document_date or "") or None,
                "rule": (
                    "This is photographed mail/business paperwork. Convert it to "
                    "an archival OCR document and file it above all child listings."
                ),
            },
            "instruction": (
                "Show this preview. If approved, call catalog_business_document "
                "again with the same upload_id/scope and confirm=yes."
            ),
        }

    staged.image.open("rb")
    try:
        data = staged.image.read()
    finally:
        staged.image.close()
    filename = Path(staged.image.name).name
    suffix = Path(filename).suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(suffix, "image/jpeg")
    upload = SimpleUploadedFile(filename, data, content_type=media_type)
    document, created = ingest_document(
        landlord=landlord,
        upload=upload,
        created_by=actor,
    )
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
    staged.used_at = timezone.now()
    staged.save(update_fields=["used_at"])
    if created:
        from .tasks import process_rama_document

        transaction.on_commit(lambda: process_rama_document.delay(str(document.pk)))
    return {
        **result,
        "promoted_from_upload_id": str(staged.pk),
        "ocr_enqueued": created,
    }


@transaction.atomic
def catalog_batch_attachment_as_document(
    landlord,
    *,
    attachment_id: str,
    scope_query: str,
    actor=None,
    issuer: str = "",
    document_date=None,
    confirm: bool = False,
) -> dict:
    """Promote one exact attachment-batch item into the document pipeline."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from .models import RamaAttachment

    staged = (
        RamaAttachment.objects.select_related("batch")
        .filter(
            pk=attachment_id,
            batch__landlord=landlord,
            status__in=[
                RamaAttachment.Status.STAGED,
                RamaAttachment.Status.CLASSIFIED,
            ],
        )
        .first()
    )
    if staged is None:
        already = RamaAttachment.objects.filter(
            pk=attachment_id,
            batch__landlord=landlord,
            status=RamaAttachment.Status.APPLIED,
            classification=RamaAttachment.Classification.DOCUMENT,
        ).first()
        if already is not None:
            return {
                "already_done": (
                    f"{already.original_filename} is already stored as document "
                    f"{already.target_id}."
                ),
                "document_id": already.target_id,
            }
        return {"error": "No available attachment with that attachment_id."}
    resolved = resolve_holding_scope(landlord, scope_query)
    if resolved.get("error"):
        return resolved
    if not confirm:
        return {
            "needs_confirm": True,
            "action": "catalog_business_document",
            "preview": {
                "attachment_batch_id": str(staged.batch_id),
                "attachment_id": str(staged.pk),
                "document": staged.original_filename,
                "scope": resolved["address"] or scope_query,
                "scope_kind": "physical_property_holding",
                "convert_to_ocr_document": True,
                "create_holding": resolved["create"],
                "child_listings": resolved["listings"],
                "issuer": issuer or None,
                "document_date": str(document_date or "") or None,
            },
            "instruction": (
                "Show this preview. On approval, call catalog_business_document "
                "again with the same attachment_id/scope and confirm=yes."
            ),
        }

    staged.original.open("rb")
    try:
        data = staged.original.read()
    finally:
        staged.original.close()
    upload = SimpleUploadedFile(
        staged.original_filename,
        data,
        content_type=staged.content_type or "application/octet-stream",
    )
    document, created = ingest_document(
        landlord=landlord,
        upload=upload,
        created_by=actor,
    )
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
    staged.classification = RamaAttachment.Classification.DOCUMENT
    staged.status = RamaAttachment.Status.APPLIED
    staged.target_type = "rama_document"
    staged.target_id = str(document.pk)
    staged.result = {"document_id": str(document.pk), "created": created}
    staged.save(
        update_fields=[
            "classification",
            "status",
            "target_type",
            "target_id",
            "result",
            "updated_at",
        ]
    )
    if created:
        from .tasks import process_rama_document

        transaction.on_commit(lambda: process_rama_document.delay(str(document.pk)))
    return {
        **result,
        "promoted_from_attachment_id": str(staged.pk),
        "attachment_batch_id": str(staged.batch_id),
        "ocr_enqueued": created,
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


def _first_money(text: str) -> Decimal | None:
    candidates = re.findall(r"(?:CAD\s*)?\$\s*([\d,]+(?:\.\d{2})?)", text, re.I)
    for raw in candidates:
        try:
            value = Decimal(raw.replace(",", ""))
            if value > 0:
                return value
        except InvalidOperation:
            continue
    return None


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
        (
            ("invoice", "receipt", "amount due", "subtotal"),
            RamaDocument.Kind.EXPENSE,
            ExpenseCategory.OTHER,
        ),
        (
            ("repair", "work order", "plumbing", "electrical"),
            RamaDocument.Kind.MAINTENANCE,
            ExpenseCategory.MAINTENANCE,
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
        if any(
            term in value
            for term in ("paid", "payment received", "receipt", "balance $0")
        ):
            payment_state = RamaDocument.PaymentState.PAID
        elif any(
            term in value
            for term in ("invoice", "amount due", "due date", "balance due")
        ):
            payment_state = RamaDocument.PaymentState.UNPAID
        else:
            payment_state = RamaDocument.PaymentState.UNKNOWN
    else:
        payment_state = RamaDocument.PaymentState.NOT_APPLICABLE

    title = {
        RamaDocument.Kind.TAX: "Property Tax Notice",
        RamaDocument.Kind.MORTGAGE: "Mortgage Document",
        RamaDocument.Kind.INSURANCE: "Insurance Document",
        RamaDocument.Kind.EXPENSE: "Expense Document",
        RamaDocument.Kind.MAINTENANCE: "Maintenance Document",
        RamaDocument.Kind.LEASE: "Lease Document",
        RamaDocument.Kind.BANK_STATEMENT: "Bank Statement",
    }.get(kind, "Business Document")
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
            else _first_money(text)
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


def process_document(document_id) -> RamaDocument:
    """OCR and propose filing metadata. Never posts money without review."""
    document = RamaDocument.objects.select_related("landlord").get(pk=document_id)
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
        document.holding = holding
        document.classification_confidence = result["confidence"]
        document.match_confidence = match_confidence
        reasons = []
        if not text.strip():
            reasons.append("I could not read enough text from the file.")
        if not holding:
            reasons.append("I could not confidently identify the property address.")
        if result["confidence"] < Decimal("0.70"):
            reasons.append("I could not confidently identify the document type.")
        if (
            result["payment_state"] == RamaDocument.PaymentState.UNKNOWN
            and result["amount"]
        ):
            reasons.append("I cannot tell whether this amount has left the bank.")
        document.clarification_question = " ".join(reasons)
        document.status = (
            RamaDocument.Status.NEEDS_REVIEW if reasons else RamaDocument.Status.READY
        )
        document.canonical_filename = _canonical_name(document)
        document.archival_pdf.save(
            _archive_path(document), ContentFile(pdf), save=False
        )
        document.extracted_data = {
            "classifier": "rentium-rules-v1",
            "ocr_characters": len(text),
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
