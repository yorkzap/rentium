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


_HINT_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "these",
        "those",
        "found",
        "here",
        "expense",
        "receipt",
        "invoice",
        "purchase",
        "bought",
        "paid",
        "just",
        "store",
        "document",
        "should",
        "know",
        "already",
        "logged",
        "recorded",
        "gift",
        "card",
        "figure",
        "about",
        "whole",
        "portfolio",
        "property",
        "address",
        "walmart",
        "costco",
        "canadian",
        "tire",
        "home",
        "depot",
        "photo",
        "picture",
        "image",
        "file",
        "please",
        "want",
        "need",
        "have",
    }
)


def _hint_tokens(*parts: str) -> list[str]:
    joined = " ".join(p for p in parts if p)
    tokens = []
    seen: set[str] = set()
    for t in re.findall(r"[a-zA-Z]{3,}", joined.casefold()):
        if t in _HINT_STOPWORDS or t in seen:
            continue
        # Normalize common brand spellings so "drano" matches "draino".
        if t in {"drano", "draino"}:
            t = "drano"
        seen.add(t)
        tokens.append(t)
    return tokens


def find_expense_to_attach_receipt(
    landlord,
    *,
    amount=None,
    amount_candidates: list | None = None,
    holding=None,
    description_hint: str = "",
    window_days: int = 60,
) -> list:
    """Find open expenses this receipt likely documents (already logged in chat).

    Match requires **amount agreement and/or description tokens**. Same holding
    alone is never enough — that was wrongly linking a $39 nozzle receipt to a
    $18 Draino expense just because both were at McKenzie.
    """
    from datetime import timedelta

    from rentium.ledger.models import EntryType, LedgerEntry

    day = date.today()
    qs = (
        LedgerEntry.objects.not_voided()
        .filter(
            landlord=landlord,
            entry_type=EntryType.EXPENSE,
            effective_date__gte=day - timedelta(days=window_days),
        )
        .select_related("holding", "property")
        .order_by("-effective_date", "-created_at")
    )
    if holding is not None:
        qs = qs.filter(holding_id=holding.pk)

    amounts: set[Decimal] = set()
    if amount not in (None, ""):
        try:
            amounts.add(Decimal(str(amount)))
        except (InvalidOperation, TypeError, ValueError):
            pass
    for raw in amount_candidates or []:
        try:
            if isinstance(raw, dict):
                raw = raw.get("amount")
            amounts.add(Decimal(str(raw)))
        except (InvalidOperation, TypeError, ValueError):
            continue

    tokens = _hint_tokens(description_hint)
    # Brand / product tokens that can stand alone without amount agreement.
    strong_tokens = {
        "drano",
        "screens",
        "mulch",
        "nozzle",
        "washer",
        "pressure",
    }

    rows = list(qs[:100])
    scored: list[tuple[int, object]] = []
    for entry in rows:
        score = 0
        amount_hit = False
        if amounts and entry.amount in amounts:
            score += 50
            amount_hit = True
        # Near-amount (tax/rounding / OCR line vs logged total) within $2.
        elif amounts:
            for a in amounts:
                if abs(entry.amount - a) <= Decimal("2.00"):
                    score += 30
                    amount_hit = True
                    break

        desc = (entry.description or "").casefold()
        desc_norm = desc.replace("draino", "drano")
        token_hits = 0
        strong_hits = 0
        for tok in tokens:
            if tok in desc_norm or tok in desc:
                token_hits += 1
                if tok in strong_tokens:
                    strong_hits += 1
                    score += 25
                else:
                    score += 12

        # Gate: property alone is not a match.
        if not amount_hit and token_hits == 0:
            continue
        # Landlord/OCR named a $ amount that disagrees: only keep if a strong
        # product token still ties them (e.g. "draino receipt" with OCR $11.97
        # vs logged $13.41 is near-amount; $39 vs $18 Draino with "nozzle" must
        # NOT match Draino).
        if amounts and not amount_hit and strong_hits == 0:
            continue

        if holding is not None and entry.holding_id == holding.pk:
            score += 10  # boost only, never the sole signal
        elif holding is None and entry.holding_id:
            pass

        # Prefer rows that still lack a source document.
        has_doc = False
        try:
            has_doc = getattr(entry, "source_document", None) is not None
        except Exception:  # noqa: BLE001
            has_doc = False
        if not has_doc:
            has_doc = RamaDocument.objects.filter(ledger_entry_id=entry.pk).exists()
        if has_doc:
            score -= 40
        if score >= 25:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], -pair[1].effective_date.toordinal()))
    return [e for _, e in scored[:8]]


def match_receipt_to_logged_expense(
    landlord,
    document: RamaDocument,
    *,
    caption: str = "",
) -> dict | None:
    """If this receipt is for an expense already on the books, return the match.

    Uses landlord caption ("the draino receipt"), OCR text/title, and OCR amount
    *candidates* (not only the max $ figure) so gift-card lines do not block a
    match to a $13 Draino expense.
    """
    data = document.extracted_data or {}
    candidates = data.get("amount_candidates") or []
    if not candidates and (document.ocr_text or ""):
        candidates = _money_candidates(document.ocr_text)
    hint = " ".join(
        part
        for part in (
            caption,
            document.title or "",
            document.issuer or "",
            (document.ocr_text or "")[:500],
        )
        if part
    )
    matches = find_expense_to_attach_receipt(
        landlord,
        amount=document.amount,
        amount_candidates=candidates,
        holding=document.holding,
        description_hint=hint,
    )
    if not matches:
        # Retry without holding filter — OCR may not have set property yet.
        matches = find_expense_to_attach_receipt(
            landlord,
            amount=document.amount,
            amount_candidates=candidates,
            holding=None,
            description_hint=hint,
        )
    # If the document has a clear amount far from the only candidate, drop it.
    if document.amount is not None and matches:
        kept = []
        for e in matches:
            if abs(e.amount - document.amount) <= Decimal("2.00"):
                kept.append(e)
                continue
            # Allow only with strong shared product token in caption/OCR vs desc.
            desc = (e.description or "").casefold().replace("draino", "drano")
            toks = _hint_tokens(hint)
            if any(
                t in desc
                for t in toks
                if t in {"drano", "screens", "mulch", "nozzle", "washer"}
            ):
                # Still require near-amount when document amount is set —
                # different products at same address are not the same expense.
                continue
            # No keep
        matches = kept
    if len(matches) != 1:
        return None
    entry = matches[0]
    # Prefer the logged amount over a gift-card OCR total.
    return {
        "expense_id": str(entry.pk),
        "amount": str(entry.amount),
        "description": (entry.description or "")[:160],
        "holding_id": str(entry.holding_id) if entry.holding_id else None,
        "holding_address": (
            entry.holding.address or entry.holding.name if entry.holding_id else None
        ),
        "effective_date": str(entry.effective_date) if entry.effective_date else None,
        "paid_on": str(entry.paid_on) if entry.paid_on else None,
        "match_reason": "caption_ocr_amount_candidates",
    }


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


def _image_bytes_to_pdf(source: bytes, media_type: str) -> bytes:
    """Raster image → single/multi-page PDF for the OCR pipeline."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise DocumentError("Document conversion support is not installed.") from exc

    try:
        if media_type in {"image/heic", "image/heif"}:
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
        buf = io.BytesIO()
        frames[0].save(
            buf,
            "PDF",
            save_all=True,
            append_images=frames[1:],
            resolution=300,
        )
        return buf.getvalue()
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"Could not convert this image: {exc}") from exc


def _run_ocrmypdf(
    input_path: Path,
    output_path: Path,
    sidecar_path: Path,
    *,
    force_ocr: bool,
    output_type: str,
) -> subprocess.CompletedProcess:
    """Run ocrmypdf once. See _pdf_and_text for why force_ocr is the default."""
    command = [
        "ocrmypdf",
        "--force-ocr" if force_ocr else "--skip-text",
        "--rotate-pages",
        "--deskew",
        "--output-type",
        output_type,
        "--language",
        "eng+fra",
        "--sidecar",
        str(sidecar_path),
        str(input_path),
        str(output_path),
    ]
    return subprocess.run(
        command, capture_output=True, text=True, timeout=240, check=False
    )


def _pdf_and_text(document: RamaDocument) -> tuple[bytes, str]:
    """Produce a searchable archival PDF + plain text.

    Ghostscript 10.0.0–10.02.0 (Debian bookworm's package) refuses ocrmypdf's
    ``--skip-text`` / PDF/A path and fails every photo invoice. Prefer
    ``--force-ocr`` (always re-OCR — correct for camera receipts) with PDF/A;
    fall back to plain PDF output if GS still chokes.
    """
    document.original_file.open("rb")
    try:
        source = document.original_file.read()
    finally:
        document.original_file.close()

    with tempfile.TemporaryDirectory(prefix="rentium-ocr-") as tmp:
        input_path = Path(tmp) / "input.pdf"
        output_path = Path(tmp) / "archive.pdf"
        sidecar_path = Path(tmp) / "ocr.txt"
        if document.media_type == "application/pdf":
            input_path.write_bytes(source)
        else:
            input_path.write_bytes(_image_bytes_to_pdf(source, document.media_type))

        # Order matters: force-ocr + pdfa works on broken GS; skip-text does not.
        attempts = (
            (True, "pdfa"),
            (True, "pdf"),
            (False, "pdf"),  # born-digital text PDFs if force somehow fails
        )
        last_detail = ""
        completed = None
        for force_ocr, output_type in attempts:
            if output_path.exists():
                output_path.unlink()
            if sidecar_path.exists():
                sidecar_path.unlink()
            try:
                completed = _run_ocrmypdf(
                    input_path,
                    output_path,
                    sidecar_path,
                    force_ocr=force_ocr,
                    output_type=output_type,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise DocumentError(f"OCR engine unavailable: {exc}") from exc
            # 0 = ok, 6 = already has text (skip-text path only)
            if completed.returncode in {0, 6}:
                break
            last_detail = (completed.stderr or completed.stdout or "").strip()[-1200:]
            low = last_detail.casefold()
            # Retry only the known Ghostscript / mode failures.
            if (
                "ghostscript" not in low
                and "skip-text" not in low
                and "force-ocr" not in low
            ):
                break

        if completed is None or completed.returncode not in {0, 6}:
            detail = last_detail or "unknown OCR error"
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
    """Match OCR text to a physical holding.

    Invoices often print the landlord's home address *and* the service address.
    Prefer an explicit SERVICE ADDRESS block when present so we do not file
    McKenzie work against a Wascana holding (or the reverse).
    """
    raw = text or ""
    service_block = ""
    m = re.search(
        r"service\s+address\s*[:\n]+(.{0,200})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        service_block = m.group(1)

    haystack_full = _normalise_address(raw)
    haystack_service = _normalise_address(service_block) if service_block else ""

    matches = []
    for holding in PropertyHolding.objects.filter(landlord=landlord):
        address = _normalise_address(holding.address)
        name = _normalise_address(holding.name)
        score = Decimal("0")
        if address:
            if haystack_service and address in haystack_service:
                score = Decimal("1.0")
            elif address in haystack_full:
                # Present somewhere on the page, but not in the service block.
                score = Decimal("0.70") if haystack_service else Decimal("1.0")
        if score == 0 and name and name in haystack_full:
            score = Decimal("0.85")
        if score:
            matches.append((holding, score))
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

    # Telegram photos are RamaUpload rows. Weak models often put that UUID in
    # attachment_id; resolve to the table that actually has the row.
    if aid and not RamaAttachment.objects.filter(
        pk=aid, batch__landlord=landlord
    ).exists():
        if RamaUpload.objects.filter(pk=aid, landlord=landlord).exists():
            uid = uid or aid
            aid = ""
    if uid and not RamaUpload.objects.filter(pk=uid, landlord=landlord).exists():
        if RamaAttachment.objects.filter(
            pk=uid, batch__landlord=landlord
        ).exists():
            aid = aid or uid
            uid = ""

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
            return {
                "error": (
                    f"No attachment {aid!r}. For Telegram photos use upload_id "
                    "(or pass the same UUID — we resolve uploads automatically)."
                ),
            }
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


# Gift-card / prepaid loads look like huge "totals" and poison max-$ extraction.
_GIFT_CARD_LINE_RE = re.compile(
    r"\b(gift\s*card|prepaid|load(?:ed)?\s*(?:card)?|visa\s*gift|"
    r"mastercard\s*gift|cash\s*card|reload)\b",
    re.I,
)
_LINE_ITEM_AMOUNT_RE = re.compile(
    r"(?P<label>[^\n$]{2,60}?)(?:\s{1,}|\$)\s*\$?\s*(?P<amt>[\d,]+(?:\.\d{2}))\b"
)


def _money_candidates(text: str) -> list[dict]:
    """Ranked money figures from a receipt/invoice with source labels.

    Gift-card / prepaid lines are kept as candidates but heavily down-ranked so
    a $1000 gift-card load does not beat a $11.97 product line.
    """
    body = text or ""
    scored: list[tuple[int, Decimal, str]] = []
    seen: set[str] = set()

    def add(value: Decimal | None, score: int, source: str) -> None:
        if value is None:
            return
        # Dedupe same amount — keep highest score, but never let bare_money
        # erase a more specific gift_card/line_item/labeled tag (needed so
        # gift-card detection survives a later bare $ pass).
        for i, (s, v, src) in enumerate(scored):
            if v != value:
                continue
            specific = {
                "gift_card_line",
                "labeled_total_gift_context",
                "line_item",
                "labeled_total",
            }
            if src in specific and source == "bare_money":
                return
            if score > s or (score == s and source in specific and src == "bare_money"):
                scored[i] = (score, value, source)
            return
        scored.append((score, value, source))

    # Labeled totals (invoice total / amount due) — strong, but not if the
    # surrounding line is a gift-card load.
    for m in re.finditer(
        r"(?P<ctx>[^\n]{0,40})(?:grand\s*total|invoice\s*total|amount\s*due|"
        r"balance\s*due|total\s*due|total\s*amount|amount\s*paid|(?<![a-z])total)"
        r"\b[^\n\d$]{0,40}\$?\s*(?P<amt>[\d,]+(?:\.\d{2})?)",
        body,
        re.I,
    ):
        value = _parse_money(m.group("amt"))
        ctx = f"{m.group('ctx')} {m.group(0)}"
        if _GIFT_CARD_LINE_RE.search(ctx):
            add(value, 10, "labeled_total_gift_context")
        else:
            add(value, 100, "labeled_total")

    # Product-ish line items: "DRANO 2.37L … $11.97" (same line or next line).
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        money_on_line = re.findall(
            r"(?:CAD\s*)?\$\s*([\d,]+(?:\.\d{2})?)|([\d,]+\.\d{2})\b", line, re.I
        )
        amounts_here = []
        for a, b in money_on_line:
            v = _parse_money(a or b)
            if v is not None:
                amounts_here.append(v)
        label = re.sub(
            r"(?:CAD\s*)?\$\s*[\d,]+(?:\.\d{2})?|[\d,]+\.\d{2}",
            " ",
            line,
            flags=re.I,
        ).strip(" -.:")
        # Amount alone on this line → pair with previous product line.
        if amounts_here and not re.search(r"[A-Za-z]{3,}", label) and idx > 0:
            label = lines[idx - 1]
        if not amounts_here:
            continue
        for value in amounts_here:
            ctx = f"{label} {line}"
            if _GIFT_CARD_LINE_RE.search(ctx):
                add(value, 5, "gift_card_line")
                continue
            if re.search(
                r"\b(grand\s*total|invoice\s*total|amount\s*due|balance\s*due|"
                r"total\s*due|subtotal|tax|hst|gst|pst|change)\b",
                ctx,
                re.I,
            ):
                continue  # handled as labeled / ignored noise
            score = 40
            if re.search(r"[A-Za-z]{3,}", label):
                score += 25
            if re.search(
                r"\d+\.?\d*\s*(L|ml|kg|g|oz|pk|ct|ea)\b", label, re.I
            ):
                score += 15
            if re.search(
                r"\b(cash|debit|credit|visa|mastercard|tender)\b", label, re.I
            ):
                score -= 25
            add(value, score, "line_item")

    # Remaining bare $ figures (lowest priority; max used only as fallback).
    for value in _all_money_values(body):
        add(value, 15, "bare_money")

    scored.sort(key=lambda row: (-row[0], -row[1]))
    return [
        {"amount": str(value), "score": score, "source": source}
        for score, value, source in scored
        if score > 0
    ]


def _extract_amount(text: str) -> Decimal | None:
    """Best-effort invoice/receipt total — not the first random $ figure.

    Prefer labeled totals (Total / Amount Due), then product line items.
    Gift-card / prepaid loads are never preferred. When a gift-card line is
    present, labeled "TOTAL" is often the gift+product sum — prefer the best
    merchandise line item instead.
    """
    candidates = _money_candidates(text)
    if not candidates:
        return None
    has_gift = any(
        c.get("source") in {"gift_card_line", "labeled_total_gift_context"}
        or "gift" in str(c.get("source") or "")
        for c in candidates
    ) or bool(_GIFT_CARD_LINE_RE.search(text or ""))
    line_items = [c for c in candidates if c.get("source") == "line_item"]
    labeled = [c for c in candidates if c.get("source") == "labeled_total"]
    if has_gift and line_items:
        # Walmart-style: DRANO $11.97 + GIFT CARD $1000 + TOTAL $1011.97
        return Decimal(line_items[0]["amount"])
    if labeled:
        return Decimal(labeled[0]["amount"])
    for row in candidates:
        if row["source"] != "gift_card_line" and not str(row["source"]).endswith(
            "gift_context"
        ):
            return Decimal(row["amount"])
    return Decimal(candidates[0]["amount"])


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
        else:
            # Vendor-style OCR: "PNR Screens Ltd" without the word "window".
            if re.search(r"\bscreens?\b", value):
                title = "Maintenance — Window Screens"
    # Prefer a company-looking vendor (… Ltd / Inc) over a personal name line.
    vendor = re.search(
        r"([A-Z][A-Za-z0-9&.' -]{1,40}\s(?:Ltd|Inc|LLC|Co\.?|Company)\.?)",
        text or "",
    )
    if vendor and kind in {
        RamaDocument.Kind.MAINTENANCE,
        RamaDocument.Kind.EXPENSE,
    }:
        vendor_name = re.sub(r"\s+", " ", vendor.group(1)).strip(" .,")
        if 4 <= len(vendor_name) <= 48:
            if title.startswith("Maintenance —"):
                title = f"{title} ({vendor_name})"
            else:
                title = f"{vendor_name} Invoice"

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


def document_intelligence_payload(
    document: RamaDocument, *, caption: str = ""
) -> dict:
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
    data = document.extracted_data or {}
    amount_candidates = data.get("amount_candidates") or []
    matching_expense = None
    if expense_like and not document.ledger_entry_id:
        matching_expense = match_receipt_to_logged_expense(
            document.landlord, document, caption=caption
        )
    next_steps: list[str] = []
    if matching_expense:
        next_steps.append(
            "MATCHED existing ledger expense "
            f"${matching_expense['amount']} — {matching_expense['description']} "
            f"at {matching_expense.get('holding_address') or 'portfolio'}. "
            "Offer to store this receipt against that expense "
            "(file_business_document with duplicate_resolution=link:"
            f"{matching_expense['expense_id']} or auto_link). "
            "Do NOT ask which property. Do NOT post a second expense."
        )
    elif expense_like and document.amount:
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
        reason = (document.failure_reason or "").casefold()
        if "ghostscript" in reason or "skip-text" in reason:
            next_steps.append(
                "OCR hit a server Ghostscript bug (not a blurry photo). "
                "Retry OCR on this document_id (or the Documents page Retry "
                "button) — do NOT ask the landlord to re-photograph a clear invoice."
            )
        else:
            next_steps.append(
                f"OCR failed: {document.failure_reason or 'unknown'}. "
                "Retry OCR once; only ask for a re-send if Retry also fails."
            )
    return {
        "document_id": str(document.pk),
        "status": document.status,
        "kind": document.kind,
        "kind_display": document.get_kind_display(),
        "title": document.title or document.original_filename,
        "amount": str(document.amount) if document.amount is not None else None,
        "amount_candidates": amount_candidates[:8],
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
        "matching_expense": matching_expense,
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
            "NEVER invent or guess the amount — use amount / amount_candidates "
            "from this payload only. If matching_expense is set, offer to attach "
            "the receipt to that expense and do NOT ask for property or post a "
            "new expense. NEVER void because an expense was paid. "
            "Paid means post with payment_state=PAID (sets paid_on). "
            "Unpaid means post with payment_state=UNPAID (paid_on null)."
        ),
    }


def _ensure_ocr(document: RamaDocument, *, force: bool = False) -> RamaDocument:
    """Run OCR now if still pending (chat path needs facts before the reply).

    FAILED rows with empty OCR (typical Ghostscript misconfig) are always
    retried. Pass force=True to re-run after a successful but wrong pass.
    """
    document.refresh_from_db()
    if document.status == RamaDocument.Status.FILED and not force:
        return document
    if (
        not force
        and document.status == RamaDocument.Status.FAILED
        and (document.ocr_text or "").strip()
    ):
        return document
    if (
        not force
        and document.status
        in {
            RamaDocument.Status.READY,
            RamaDocument.Status.NEEDS_REVIEW,
        }
        and (document.ocr_text or "").strip()
    ):
        return document
    return process_document(document.pk)


def reocr_document(*, landlord, document) -> RamaDocument:
    """Re-run OCR on a document the landlord still owns (inbox Retry button)."""
    if document.landlord_id != landlord.pk:
        raise DocumentError("Document not found.")
    if document.status == RamaDocument.Status.FILED and document.ledger_entry_id:
        # Still allow re-OCR for better text search; filing stays linked.
        pass
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
        amount_candidates = (
            []
            if result["kind"] == RamaDocument.Kind.BANK_STATEMENT
            else _money_candidates(text)
        )
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
        # Ambiguous amounts (gift card + product lines) — surface for matching.
        non_gift = [
            c
            for c in amount_candidates
            if c.get("source") not in {"gift_card_line", "labeled_total_gift_context"}
        ]
        if len(non_gift) >= 2 and result["amount"] is not None:
            top = Decimal(non_gift[0]["amount"])
            second = Decimal(non_gift[1]["amount"])
            if top != second and abs(top - second) > Decimal("5"):
                reasons.append(
                    "Several money figures appear on this slip; confirm the "
                    "real purchase total if OCR is wrong."
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
            "amount_extractor": "ranked_candidates_v1",
            "amount_candidates": amount_candidates[:12],
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
    """Chat wrapper around file_document: post expense from OCR with paid/unpaid.

    `duplicate_resolution`:
    - "" / default — refuse if same-cost candidates (or post new)
    - "new" — post a separate expense
    - "link:<id>" — attach receipt to that entry only
    - "auto_link" — pick the best matching existing expense and link (no post)
    """

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
    resolution = (duplicate_resolution or "").strip()
    auto_link = resolution.casefold() in {
        "auto_link",
        "auto-link",
        "link",
        "existing",
    }

    amt = amount.strip() if amount else ""
    if not amt and document.amount is not None:
        amt = str(document.amount)

    # Auto-link / explicit link: do not require payment_state (expense already exists).
    if auto_link or resolution.startswith("link:"):
        if not document.holding_id and not document.portfolio_wide:
            return {
                "needs_input": True,
                "question_for_user": (
                    "Which physical property is this receipt for? "
                    "(Needed to match the existing expense.)"
                ),
                "intelligence": document_intelligence_payload(document),
            }
        link_id = ""
        entry_probe = None
        if resolution.startswith("link:"):
            link_id = resolution.split(":", 1)[1].strip()
        else:
            hint = " ".join(
                part
                for part in (
                    document.title or "",
                    document.issuer or "",
                    (document.ocr_text or "")[:200],
                )
                if part
            )
            data = document.extracted_data or {}
            matches = find_expense_to_attach_receipt(
                landlord,
                amount=amt or None,
                amount_candidates=data.get("amount_candidates") or [],
                holding=document.holding,
                description_hint=hint,
            )
            if len(matches) == 1:
                link_id = str(matches[0].pk)
                entry_probe = matches[0]
            elif len(matches) > 1:
                return {
                    "error": "Several expenses could match this receipt.",
                    "code": "AMBIGUOUS_EXPENSE",
                    "candidates": [
                        {
                            "id": str(e.pk),
                            "amount": str(e.amount),
                            "description": (e.description or "")[:120],
                            "effective_date": str(e.effective_date),
                            "scope": (
                                e.holding.address
                                if e.holding_id
                                else (e.property.name if e.property_id else "")
                            ),
                        }
                        for e in matches[:5]
                    ],
                    "resolutions": {
                        "link": "duplicate_resolution=link:<entry_id>",
                    },
                }
            else:
                return {
                    "error": (
                        "No matching open expense found to attach this receipt to. "
                        "Say the amount/description of the logged expense, or file "
                        "as a new expense with paid/unpaid."
                    ),
                    "code": "NO_MATCHING_EXPENSE",
                }
        # Payment state mirrors the existing entry once linked.
        from rentium.ledger.models import LedgerEntry

        if entry_probe is None:
            entry_probe = LedgerEntry.objects.filter(
                pk=link_id, landlord=landlord
            ).first()
        pay = (
            RamaDocument.PaymentState.PAID
            if entry_probe and entry_probe.paid_on
            else RamaDocument.PaymentState.UNPAID
        )
        if not amt and entry_probe is not None:
            amt = str(entry_probe.amount)
        if not amt:
            return {
                "needs_input": True,
                "question_for_user": "What total should we store on the receipt?",
                "intelligence": document_intelligence_payload(document),
            }
        preview = {
            "document_id": str(document.pk),
            "action": "link_receipt_to_existing_expense",
            "amount": amt,
            "expense_id": link_id,
            "expense_description": (
                (entry_probe.description or "")[:120] if entry_probe else ""
            ),
            "holding": (
                document.holding.address or document.holding.name
                if document.holding_id
                else None
            ),
            "side_effects": [
                "File the document",
                "Link to existing ledger expense (no new post)",
            ],
            "never": "Never post a second expense for the same cost.",
        }
        if not _confirmed(confirm):
            return _preview(
                "file_business_document",
                preview,
                "Attaches this receipt to the existing expense only.",
            )
        try:
            filed = file_document(
                document,
                actor=getattr(landlord, "user", None),
                holding=document.holding,
                kind=document.kind or RamaDocument.Kind.EXPENSE,
                title=(title or document.title or "Receipt")[:255],
                amount=amt,
                expense_category=(
                    expense_category
                    or document.expense_category
                    or ExpenseCategory.OTHER
                ),
                payment_state=pay,
                document_date=document.document_date,
                issuer=issuer if issuer != "" else document.issuer,
                duplicate_resolution=f"link:{link_id}",
            )
        except DocumentError as exc:
            return {"error": str(exc)}
        entry = filed.ledger_entry
        return {
            "filed": True,
            "linked_existing": True,
            "document_id": str(filed.pk),
            "ledger_entry_id": str(entry.pk) if entry else link_id,
            "amount": str(entry.amount) if entry else amt,
            "paid_on": str(entry.paid_on) if entry and entry.paid_on else None,
            "holding": (
                filed.holding.address or filed.holding.name
                if filed.holding_id
                else None
            ),
            "documents_page": url_for_path(
                f"/dashboard/documents?document={filed.pk}"
            ),
            "message": (
                f"Stored the receipt and linked it to the existing "
                f"${entry.amount if entry else amt} expense"
                f" ({(entry.description if entry else '')[:80]}) — "
                f"no second expense posted."
            ),
            "intelligence": document_intelligence_payload(filed),
        }

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


# ---------------------------------------------------------------------------
# Library: search, tags, intentional delete
# ---------------------------------------------------------------------------


def _search_token_filter(q: str):
    """Match any significant token across title/issuer/OCR (landlord-friendly).

    OCR often reads 'PNR Screens' not 'window screens' — requiring every word
    (websearch AND) would miss the real invoice. Any token ≥3 chars is enough.
    """
    from django.db.models import Q

    tokens = [t for t in re.findall(r"[a-zA-Z0-9]+", q) if len(t) >= 3]
    if not tokens:
        tokens = [q]
    token_q = Q()
    for token in tokens:
        token_q |= (
            Q(title__icontains=token)
            | Q(issuer__icontains=token)
            | Q(reference_number__icontains=token)
            | Q(ocr_text__icontains=token)
            | Q(canonical_filename__icontains=token)
            | Q(original_filename__icontains=token)
        )
    # Also try the full phrase when useful (exact vendor names, etc.).
    if len(q) >= 3:
        token_q |= (
            Q(title__icontains=q)
            | Q(issuer__icontains=q)
            | Q(ocr_text__icontains=q)
        )
    return token_q


def _apply_document_search(queryset, q: str):
    """Full-text on Postgres plus token icontains (OCR-noise tolerant)."""
    q = (q or "").strip()
    if not q:
        return queryset, False

    from django.db import connection
    from django.db.models import Q

    fallback = _search_token_filter(q)

    if connection.vendor == "postgresql":
        from django.contrib.postgres.search import SearchQuery
        from django.contrib.postgres.search import SearchRank
        from django.contrib.postgres.search import SearchVector

        vector = (
            SearchVector("title", weight="A")
            + SearchVector("issuer", weight="A")
            + SearchVector("reference_number", weight="B")
            + SearchVector("canonical_filename", weight="B")
            + SearchVector("original_filename", weight="C")
            + SearchVector("ocr_text", weight="C")
        )
        query = SearchQuery(q, config="english", search_type="websearch")
        ranked = (
            queryset.annotate(search=vector, rank=SearchRank(vector, query))
            .filter(Q(search=query) | fallback)
            .order_by("-rank", "-created_at")
        )
        return ranked, True

    return queryset.filter(fallback), False


def query_business_documents(
    landlord,
    *,
    q: str = "",
    holding_id: str = "",
    kind: str = "",
    year: str | int | None = None,
    status: str = "",
    tag: str = "",
    payment_state: str = "",
    has_expense: str | bool | None = None,
    page: int = 1,
    page_size: int = 25,
):
    """Filter + full-text search the landlord's business document cabinet."""
    from django.db.models import Prefetch

    from .models import DocumentTag

    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 25)))

    include_trash = str(status or "").strip().upper() == "TRASH"
    queryset = (
        RamaDocument.objects.filter(landlord=landlord)
        .select_related("holding", "property", "ledger_entry")
        .prefetch_related(
            Prefetch(
                "tags",
                queryset=DocumentTag.objects.filter(landlord=landlord).order_by("name"),
            )
        )
    )
    if include_trash:
        queryset = queryset.exclude(deleted_at__isnull=True)
    else:
        queryset = queryset.filter(deleted_at__isnull=True)

    status_filter = str(status or "").strip().upper()
    if status_filter and status_filter != "TRASH":
        queryset = queryset.filter(status=status_filter)

    kind_filter = str(kind or "").strip().upper()
    if kind_filter:
        queryset = queryset.filter(kind=kind_filter)

    payment = str(payment_state or "").strip().upper()
    if payment:
        queryset = queryset.filter(payment_state=payment)

    holding = str(holding_id or "").strip()
    if holding:
        queryset = queryset.filter(holding_id=holding)

    if year not in (None, ""):
        try:
            year_int = int(year)
        except (TypeError, ValueError) as exc:
            raise DocumentError("year must be a four-digit number.") from exc
        queryset = queryset.filter(document_date__year=year_int)

    tag_filter = str(tag or "").strip().lower()
    if tag_filter:
        queryset = queryset.filter(tags__slug=tag_filter).distinct()

    if has_expense is not None and has_expense != "":
        want = has_expense
        if isinstance(want, str):
            want = want.strip().lower() in {"1", "true", "yes", "y"}
        if want:
            queryset = queryset.filter(ledger_entry__isnull=False)
        else:
            queryset = queryset.filter(ledger_entry__isnull=True)

    queryset, ranked = _apply_document_search(queryset, q)
    if not ranked:
        queryset = queryset.order_by("-created_at")

    total = queryset.count()
    start = (page - 1) * page_size
    rows = list(queryset[start : start + page_size])
    return {
        "documents": rows,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_next": start + page_size < total,
            "has_prev": page > 1,
        },
    }


def search_business_documents_for_chat(
    landlord,
    *,
    query: str = "",
    holding_query: str = "",
    kind: str = "",
    year: str = "",
    tag: str = "",
    status: str = "",
    payment_state: str = "",
    has_expense: str = "",
    limit: int = 20,
) -> dict:
    """RAMA read tool: find filed paperwork by text + filters."""
    holding_id = ""
    hq = (holding_query or "").strip()
    if hq:
        holdings = PropertyHolding.objects.filter(landlord=landlord)
        exact = holdings.filter(address__iexact=hq).first()
        if exact is None:
            exact = holdings.filter(name__iexact=hq).first()
        if exact is None:
            matches = list(
                holdings.filter(address__icontains=hq)[:5]
            ) or list(holdings.filter(name__icontains=hq)[:5])
            if len(matches) == 1:
                exact = matches[0]
            elif len(matches) > 1:
                return {
                    "ok": False,
                    "error": "Several holdings match that address/name.",
                    "candidates": [
                        {
                            "id": str(h.pk),
                            "name": h.name,
                            "address": h.address,
                        }
                        for h in matches
                    ],
                    "hint": "Pass a more specific holding_query.",
                }
            else:
                return {
                    "ok": False,
                    "error": f"No holding matches {hq!r}.",
                }
        holding_id = str(exact.pk)

    try:
        result = query_business_documents(
            landlord,
            q=query,
            holding_id=holding_id,
            kind=kind,
            year=year or None,
            status=status,
            tag=tag,
            payment_state=payment_state,
            has_expense=has_expense if has_expense else None,
            page=1,
            page_size=min(50, max(1, int(limit or 20))),
        )
    except DocumentError as exc:
        return {"ok": False, "error": str(exc)}

    rows = []
    for doc in result["documents"]:
        rows.append(
            {
                "document_id": str(doc.pk),
                "title": doc.get_display_title(),
                "kind": doc.kind,
                "status": doc.status,
                "issuer": doc.issuer,
                "reference_number": doc.reference_number,
                "document_date": (
                    str(doc.document_date) if doc.document_date else None
                ),
                "amount": str(doc.amount) if doc.amount is not None else None,
                "currency": doc.currency,
                "payment_state": doc.payment_state,
                "holding": (
                    {
                        "id": str(doc.holding_id),
                        "name": doc.holding.name if doc.holding_id else None,
                        "address": doc.holding.address if doc.holding_id else None,
                    }
                    if doc.holding_id
                    else None
                ),
                "tags": [t.slug for t in doc.tags.all()],
                "ledger_entry_id": (
                    str(doc.ledger_entry_id) if doc.ledger_entry_id else None
                ),
                "documents_page": url_for_path(
                    f"/dashboard/documents?document={doc.pk}"
                ),
            }
        )
    return {
        "ok": True,
        "query": (query or "").strip(),
        "count": result["pagination"]["total"],
        "returned": len(rows),
        "documents": rows,
        "documents_page": url_for_path("/dashboard/documents"),
    }


def get_or_create_document_tag(landlord, name: str):
    from .models import DocumentTag

    raw = (name or "").strip()
    if not raw:
        raise DocumentError("Tag name is required.")
    slug = slugify(raw)[:64] or "tag"
    tag, _ = DocumentTag.objects.get_or_create(
        landlord=landlord,
        slug=slug,
        defaults={"name": raw[:64]},
    )
    return tag


def set_document_tags(document, tag_names: list[str], *, replace: bool = True):
    """Attach labels to a document. Names are created if missing for that landlord."""
    tags = [get_or_create_document_tag(document.landlord, n) for n in tag_names if n]
    if replace:
        document.tags.set(tags)
    else:
        document.tags.add(*tags)
    return list(document.tags.order_by("name"))


def list_document_tags(landlord):
    from .models import DocumentTag
    from django.db.models import Count

    return list(
        DocumentTag.objects.filter(landlord=landlord)
        .annotate(document_count=Count("documents"))
        .order_by("name")
    )


def delete_document(*, landlord, document, hard: bool = False) -> dict:
    """Move a document to trash (soft) or permanently remove (hard).

    Soft-delete is the default — hides from the library; restore anytime.
    Linked ledger expenses are never deleted (append-only audit). Hard delete
    is refused while a ledger link exists.
    """
    if document.landlord_id != landlord.pk:
        raise DocumentError("Document not found.")
    if document.ledger_entry_id and hard:
        raise DocumentError(
            "Cannot hard-delete a document linked to a ledger expense. "
            "The expense stays as audit history — void it separately if needed."
        )

    pk = str(document.pk)
    if not hard:
        if document.deleted_at:
            return {"deleted": True, "trashed": True, "document_id": pk, "already": True}
        document.deleted_at = timezone.now()
        document.save(update_fields=["deleted_at", "updated_at"])
        return {
            "deleted": True,
            "trashed": True,
            "document_id": pk,
            "ledger_entry_id": (
                str(document.ledger_entry_id) if document.ledger_entry_id else None
            ),
        }

    for field in (document.original_file, document.archival_pdf):
        if field:
            try:
                field.delete(save=False)
            except Exception:  # noqa: BLE001 — storage best-effort
                pass

    with transaction.atomic():
        document.events.all().delete()
        document.tags.clear()
        document.delete()
    return {"deleted": True, "hard": True, "document_id": pk}


def restore_document(*, landlord, document) -> dict:
    if document.landlord_id != landlord.pk:
        raise DocumentError("Document not found.")
    if not document.deleted_at:
        return {"restored": True, "document_id": str(document.pk), "already": True}
    # Unique sha256 among live docs — refuse if a live twin exists.
    twin = (
        RamaDocument.objects.filter(
            landlord=landlord, sha256=document.sha256, deleted_at__isnull=True
        )
        .exclude(pk=document.pk)
        .first()
    )
    if twin is not None:
        raise DocumentError(
            f"A live document with the same file already exists ({twin.pk})."
        )
    document.deleted_at = None
    document.save(update_fields=["deleted_at", "updated_at"])
    return {"restored": True, "document_id": str(document.pk)}


def mark_document_expense_paid(*, landlord, document, paid_on=None) -> dict:
    """Mark the linked ledger expense paid (and sync document.payment_state)."""
    from datetime import date as date_cls

    from rentium.ledger import services as ledger_services

    if document.landlord_id != landlord.pk:
        raise DocumentError("Document not found.")
    if not document.ledger_entry_id:
        raise DocumentError("This document has no linked ledger expense yet.")
    entry = document.ledger_entry
    when = paid_on or date_cls.today()
    if isinstance(when, str):
        when = date_cls.fromisoformat(when[:10])
    entry = ledger_services.mark_expense_paid(entry, paid_on=when)
    document.payment_state = RamaDocument.PaymentState.PAID
    document.save(update_fields=["payment_state", "updated_at"])
    return {
        "updated": True,
        "document_id": str(document.pk),
        "ledger_entry_id": str(entry.pk),
        "paid_on": str(entry.paid_on) if entry.paid_on else None,
        "payment_state": document.payment_state,
    }


def move_document_holding(
    *, landlord, document, holding=None, portfolio_wide: bool = False
) -> dict:
    """Re-file a document (and reallocate its expense) to another holding."""
    from rentium.ledger import services as ledger_services
    from rentium.properties.models import PropertyHolding

    if document.landlord_id != landlord.pk:
        raise DocumentError("Document not found.")
    if portfolio_wide:
        document.holding = None
        document.property = None
        document.portfolio_wide = True
    else:
        if holding is None:
            raise DocumentError("holding is required (or portfolio_wide=true).")
        if isinstance(holding, str):
            holding = PropertyHolding.objects.filter(
                pk=holding, landlord=landlord
            ).first()
        if holding is None or holding.landlord_id != landlord.pk:
            raise DocumentError("No such holding.")
        document.holding = holding
        document.property = None
        document.portfolio_wide = False

    document.canonical_filename = _canonical_name(document)
    try:
        _relocate_archive(document)
    except Exception:  # noqa: BLE001
        pass
    document.full_clean()
    document.save()

    if document.ledger_entry_id and not portfolio_wide and document.holding_id:
        entry = document.ledger_entry
        if entry and not entry.voided and entry.holding_id != document.holding_id:
            try:
                replacement = ledger_services.reallocate_entry(
                    entry,
                    property=None,
                    holding=document.holding,
                    reason="Document re-filed to correct physical property.",
                    created_by=getattr(landlord, "user", None),
                )
                document.ledger_entry = replacement
                document.save(update_fields=["ledger_entry", "updated_at"])
            except Exception as exc:  # noqa: BLE001
                return {
                    "updated": True,
                    "document_id": str(document.pk),
                    "holding_id": str(document.holding_id) if document.holding_id else None,
                    "warning": f"Document moved; expense reallocate failed: {exc}",
                }

    return {
        "updated": True,
        "document_id": str(document.pk),
        "holding_id": str(document.holding_id) if document.holding_id else None,
        "holding_name": document.holding.name if document.holding_id else None,
        "portfolio_wide": document.portfolio_wide,
        "canonical_filename": document.canonical_filename,
    }


def bulk_document_action(
    *,
    landlord,
    document_ids: list,
    action: str,
    tag_names: list | None = None,
    holding_id: str = "",
    portfolio_wide: bool = False,
) -> dict:
    """Bulk tag / trash / restore / move for the document library."""
    action = (action or "").strip().lower()
    ids = [str(i) for i in (document_ids or []) if i]
    if not ids:
        raise DocumentError("document_ids required.")
    if action not in {"trash", "restore", "tag", "move", "hard_delete"}:
        raise DocumentError(
            "action must be trash, restore, tag, move, or hard_delete."
        )

    qs = RamaDocument.objects.filter(landlord=landlord, pk__in=ids)
    if action == "restore":
        qs = qs.exclude(deleted_at__isnull=True)
    elif action != "hard_delete":
        qs = qs.filter(deleted_at__isnull=True)

    results = []
    for doc in qs:
        try:
            if action == "trash":
                results.append(delete_document(landlord=landlord, document=doc))
            elif action == "hard_delete":
                results.append(
                    delete_document(landlord=landlord, document=doc, hard=True)
                )
            elif action == "restore":
                results.append(restore_document(landlord=landlord, document=doc))
            elif action == "tag":
                set_document_tags(doc, tag_names or [], replace=False)
                results.append({"document_id": str(doc.pk), "tagged": True})
            elif action == "move":
                results.append(
                    move_document_holding(
                        landlord=landlord,
                        document=doc,
                        holding=holding_id or None,
                        portfolio_wide=portfolio_wide,
                    )
                )
        except DocumentError as exc:
            results.append({"document_id": str(doc.pk), "error": str(exc)})
    return {"action": action, "count": len(results), "results": results}
