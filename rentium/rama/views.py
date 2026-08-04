"""
RAMA HTTP surface — thin adapters over the turn engine in service.py.

Preferences (enabled / provider / model / optional BYOK api_key) are
per-landlord. Chat memory is rebuilt from this landlord's RamaAudit rows.
The engine itself (roles, confirm state machine, grounding) lives in
service.run_turn so Telegram webhooks, scheduled analyses, and delegation
call the exact same code path.
"""

import uuid
from datetime import date
from decimal import Decimal
from decimal import InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import RamaPreferences
from .providers import PROVIDERS
from .runtime import MODEL_CATALOG
from .runtime import get_landlord_config
from .runtime import get_role_config
from .runtime import platform_api_key
from .service import MAX_MESSAGE_CHARS
from .service import PENDING_ACTION_TTL_SECONDS
from .service import run_turn
from .union import state_of_the_union

__all__ = [
    "PENDING_ACTION_TTL_SECONDS",  # re-exported for tests/back-compat
    "attachment_batches_view",
    "attachment_detail_view",
    "auto_action_undo_view",
    "auto_actions_view",
    "bank_balances_view",
    "chat_view",
    "config_view",
    "constitution_view",
    "general_chat_view",
    "holdings_view",
    "insight_detail_view",
    "insights_view",
    "memory_delete_view",
    "memory_view",
    "settings_view",
    "treasurer_chat_view",
    "treasurer_view",
    "union_view",
    "upload_view",
]


def _landlord(request):
    # Resolves the portfolio the user acts on: their own, OR — for a co-landlord
    # / property manager — the owner they manage (users/access.py). A request
    # may pass ?as=<owner_id> to pick among portfolios they're allowed.
    from rentium.users.access import acting_landlord

    owner_id = request.query_params.get("as") if hasattr(request, "query_params") else None
    profile = acting_landlord(request.user, owner_id=owner_id)
    if profile is None:
        raise PermissionDenied("Landlords only.")
    return profile


def _settings_payload(landlord, prefs: RamaPreferences | None = None) -> dict:
    prefs = prefs or RamaPreferences.for_landlord(landlord)
    cfg = get_landlord_config(landlord)
    return {
        "enabled": prefs.enabled,
        "provider": prefs.provider,
        "model": prefs.model or cfg.model,
        "has_api_key": bool((prefs.api_key or "").strip()),
        "configured": cfg.is_configured(),
        "providers": sorted(PROVIDERS),
        "models": MODEL_CATALOG,
        # Provider is always selectable; "ready" means it can actually be
        # called right now: the landlord's BYOK key (which only applies to
        # their chosen provider) or a platform key. A provider with neither
        # must NOT read as ready just because it is currently selected.
        "platform_ready": {
            name: bool(platform_api_key(name))
            or (name == prefs.provider and bool((prefs.api_key or "").strip()))
            for name in PROVIDERS
        },
        # All providers available when landlord can paste a key.
        "byok": True,
        # The decision-layer (General) + analysis (FSA) roles. Blank provider =
        # "use my main model". Keys are never returned — only has_*_key.
        "general": {
            "provider": prefs.general_provider,
            "model": prefs.general_model,
            "has_key": bool((prefs.general_api_key or "").strip()),
        },
        "fsa": {
            "provider": prefs.fsa_provider,
            "model": prefs.fsa_model,
            "has_key": bool((prefs.fsa_api_key or "").strip()),
        },
        "treasurer": {
            "provider": prefs.treasurer_provider,
            "model": prefs.treasurer_model,
            "has_key": bool((prefs.treasurer_api_key or "").strip()),
        },
    }


def _apply_role_prefs(prefs, data, role: str):
    """Write general_*/fsa_* provider/model/api_key from a PATCH. Provider ''
    clears the override (role falls back to the main model)."""
    block = data.get(role)
    if not isinstance(block, dict):
        return None
    if "provider" in block:
        prov = str(block.get("provider") or "").strip().lower()
        if prov and prov not in PROVIDERS:
            return f"Unknown {role} provider {prov!r}."
        setattr(prefs, f"{role}_provider", prov)
    if "model" in block:
        setattr(prefs, f"{role}_model", str(block.get("model") or "").strip())
    if "api_key" in block:
        raw = block.get("api_key")
        if raw is None:
            setattr(prefs, f"{role}_api_key", "")
        elif str(raw).strip():
            setattr(prefs, f"{role}_api_key", str(raw).strip())
        # empty string → keep existing
    if block.get("clear_api_key"):
        setattr(prefs, f"{role}_api_key", "")
    return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portfolios_view(request):
    """GET /api/rama/portfolios/ — the portfolios this user can act as (their own
    + any they co-host), plus which one is currently active. Drives the RAMA
    'managing: [owner ▾]' switcher; every RAMA call may pass ?as=<owner_id>."""
    from rentium.users.access import actable_portfolios

    portfolios = actable_portfolios(request.user)
    acting = _landlord(request)  # honours ?as=, applies the smart default
    return Response(
        {
            "portfolios": portfolios,
            "acting_as": str(acting.pk),
            "acting_name": (getattr(acting.user, "name", "") or acting.user.email),
        },
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def union_view(request):
    """GET /api/rama/state-of-the-union/ — works without RAMA enabled."""
    return Response(state_of_the_union(_landlord(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def config_view(request):
    """GET /api/rama/config/ — panel payload for this landlord."""
    landlord = _landlord(request)
    cfg = get_landlord_config(landlord)
    return Response(
        {
            "enabled": cfg.enabled,
            "configured": cfg.is_configured(),
            "provider": cfg.provider,
            "model": cfg.model,
            "has_api_key": cfg.has_own_key,
            "providers": sorted(PROVIDERS),
            "models": MODEL_CATALOG,
            "can_override": False,
            "byok": True,
            "platform_ready": {
                name: bool(platform_api_key(name)) for name in PROVIDERS
            },
        },
    )


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def settings_view(request):
    """GET/PATCH /api/rama/settings/ — enable, model, and optional API key."""
    landlord = _landlord(request)
    prefs = RamaPreferences.for_landlord(landlord)

    if request.method == "GET":
        return Response(_settings_payload(landlord, prefs))

    data = request.data or {}
    if "enabled" in data:
        prefs.enabled = bool(data.get("enabled"))

    provider = data.get("provider")
    if provider is not None:
        provider = str(provider).strip().lower()
        if provider not in PROVIDERS:
            return Response(
                {"detail": f"Unknown provider {provider!r}."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        prefs.provider = provider

    if "model" in data:
        prefs.model = str(data.get("model") or "").strip()

    # api_key: blank string means "keep existing"; explicit null clears.
    if "api_key" in data:
        raw = data.get("api_key")
        if raw is None:
            prefs.api_key = ""
        else:
            raw = str(raw).strip()
            if raw:
                prefs.api_key = raw
            # empty string → keep existing (don't wipe on accidental blank save)

    if "clear_api_key" in data and data.get("clear_api_key"):
        prefs.api_key = ""

    # Optional per-role (decision-layer / analysis) model config.
    for role in ("general", "fsa", "treasurer"):
        role_err = _apply_role_prefs(prefs, data, role)
        if role_err:
            return Response(
                {"detail": role_err}, status=http_status.HTTP_400_BAD_REQUEST,
            )

    prefs.save()
    return Response(_settings_payload(landlord, prefs))


def _validated_chat_input(request):
    """(message, conversation_id, error_response) shared by chat endpoints."""
    message = str(request.data.get("message") or "").strip()
    if not message:
        return None, None, Response(
            {"detail": "message is required."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if len(message) > MAX_MESSAGE_CHARS:
        return None, None, Response(
            {"detail": f"message is limited to {MAX_MESSAGE_CHARS} characters."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    raw_conversation = request.data.get("conversation_id")
    try:
        conversation_id = (
            uuid.UUID(str(raw_conversation)) if raw_conversation else None
        )
    except ValueError:
        return None, None, Response(
            {"detail": "conversation_id must be a UUID."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    return message, conversation_id, None


def _chat_response(result) -> Response:
    if result.error is not None:
        status_code = result.error.get("status_hint") or 502
        return Response(
            {
                "detail": result.error["detail"],
                "code": result.error.get("code", "PROVIDER_ERROR"),
                "provider": result.provider,
                "model": result.model,
            },
            status=status_code,
        )
    return Response(
        {
            "conversation_id": str(result.conversation_id),
            "reply": result.reply,
            "provider": result.provider,
            "model": result.model,
            "tools_used": result.tools_used,
            "pending_plan": result.pending_plan,
            "auto_executed": result.auto_executed,
            "attachments": result.attachments,
        },
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def upload_view(request):
    """POST /api/rama/upload/ (multipart 'image') — stage a photo the landlord
    attached in chat. Returns upload_id to pass back on the next chat message so
    RAMA can attach_photo_to_listing it. Landlord-scoped."""
    from django.core.exceptions import ValidationError

    from .models import RamaUpload

    landlord = _landlord(request)
    f = request.FILES.get("image")
    if not f:
        return Response(
            {"detail": "An image file is required."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if f.size > 15 * 1024 * 1024:
        return Response(
            {"detail": "Image too large (max 15MB)."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    upload = RamaUpload(landlord=landlord, image=f)
    try:
        upload.full_clean()  # ImageField validation rejects non-images
        upload.save()
    except ValidationError:
        return Response(
            {"detail": "That file isn't a valid image."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    return Response(
        {"upload_id": str(upload.pk)}, status=http_status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def attachment_batches_view(request):
    """Stage exactly the files currently selected in the chat composer."""
    from .attachment_services import AttachmentError
    from .attachment_services import batch_payload
    from .attachment_services import stage_files

    landlord = _landlord(request)
    raw_conversation = request.data.get("conversation_id")
    try:
        conversation_id = (
            uuid.UUID(str(raw_conversation)) if raw_conversation else uuid.uuid4()
        )
    except ValueError:
        return Response(
            {"detail": "conversation_id must be a UUID."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    uploads = request.FILES.getlist("files")
    if not uploads:
        one = request.FILES.get("file")
        uploads = [one] if one else []
    try:
        batch = stage_files(
            landlord=landlord,
            conversation_id=conversation_id,
            uploads=uploads,
            batch_id=str(request.data.get("batch_id") or ""),
        )
    except AttachmentError as exc:
        return Response(
            {"detail": str(exc)},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    return Response(batch_payload(batch), status=http_status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def attachment_detail_view(request, attachment_id):
    from .attachment_services import AttachmentError
    from .attachment_services import batch_payload
    from .attachment_services import remove_staged_attachment

    try:
        batch = remove_staged_attachment(
            landlord=_landlord(request),
            attachment_id=attachment_id,
        )
    except AttachmentError as exc:
        return Response(
            {"detail": str(exc)},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    return Response(batch_payload(batch))


def _attachment_batch_note(request, landlord, conversation_id, caption: str = "") -> str:
    from .attachment_services import AttachmentError
    from .attachment_services import batch_chat_note
    from .attachment_services import seal_batch

    batch_id = str(request.data.get("attachment_batch_id") or "").strip()
    if not batch_id:
        return ""
    if conversation_id is None:
        raise AttachmentError(
            "conversation_id is required when sending an attachment batch.",
        )
    batch = seal_batch(
        landlord=landlord,
        conversation_id=conversation_id,
        batch_id=batch_id,
    )
    # The message sent alongside the file decides where it should go: an RTB-8
    # and an invoice are indistinguishable as bytes, but "get Sarah to sign
    # this" is not ambiguous at all.
    return batch_chat_note(batch, caption=caption)


def _attachment_note(request, landlord) -> str:
    """If the chat request references staged uploads (this landlord's, unused),
    return a note that tells the weak model the photo(s) are attached and how to
    use them. Empty string when there are none."""
    from .models import RamaUpload

    raw = request.data.get("upload_ids")
    if raw is None:
        raw = request.data.get("upload_id") or ""
    if isinstance(raw, (list, tuple)):
        ids = [str(x).strip() for x in raw if str(x).strip()]
    else:
        ids = [x.strip() for x in str(raw).split(",") if x.strip()]
    if not ids:
        return ""
    valid = [
        str(pk)
        for pk in RamaUpload.objects.filter(
            landlord=landlord, used_at__isnull=True, pk__in=ids,
        ).values_list("pk", flat=True)
    ]
    if not valid:
        return ""
    tags = " ".join(f"[The landlord attached a photo, upload_id={u}]" for u in valid)
    return (
        f"\n\n{tags}\n"
        "DEFAULT: treat attached photo(s) as a business document (receipt/"
        "invoice/notice/mail). Call catalog_business_document with upload_id "
        "ONLY first (no scope_query) so OCR runs. Do NOT assume listing or "
        "inspection photo. Do NOT say it 'looks like a property photo'. "
        "Use attach_photo_to_listing ONLY if the landlord clearly said gallery/"
        "listing/main photo/for Room X. After OCR, if they name a street "
        "address, set scope_query to that address — NEVER force a room/unit."
    )


def _document_attachment_note(request, landlord) -> str:
    from .models import RamaDocument

    raw = request.data.get("document_ids") or []
    ids = raw if isinstance(raw, (list, tuple)) else [raw]
    rows = RamaDocument.objects.filter(landlord=landlord, pk__in=ids)
    notes = []
    for row in rows:
        note = (
            f"[Business document {row.pk}: status={row.status}, "
            f"type={row.kind}, title={row.title or row.original_filename}"
        )
        if row.holding_id:
            note += f", property holding={row.holding.name}"
        if row.amount is not None:
            note += f", amount={row.currency} {row.amount}, payment={row.payment_state}"
        if row.clarification_question:
            note += f", clarification needed={row.clarification_question}"
        notes.append(note + "]")
    if not notes:
        return ""
    return (
        "\n\n"
        + "\n".join(notes)
        + "\nThis is a business-record ingestion, NOT a listing photo. Explain "
        "the current result. If the landlord names a street address/property "
        "overall, call catalog_business_document; never force a room/unit choice. "
        "If it needs review, ask the clarification question and direct the landlord "
        "to Documents to confirm before any financial entry is posted."
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def chat_view(request):
    """POST /api/rama/chat/ {message, conversation_id?, upload_ids?} — the Corporal."""
    landlord = _landlord(request)
    cfg = get_landlord_config(landlord)

    if not cfg.enabled:
        return Response(
            {
                "detail": "RAMA is turned off for your account. "
                "Enable it under Account → RAMA.",
            },
            status=http_status.HTTP_403_FORBIDDEN,
        )
    if not cfg.is_configured():
        return Response(
            {
                "detail": (
                    "Add your API key under Account → RAMA (e.g. an xAI Grok key), "
                    "then try again."
                ),
            },
            status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    message, conversation_id, err = _validated_chat_input(request)
    if err is not None:
        return err

    # If the landlord attached photo(s) in the chat, tell the model so it can
    # attach_photo_to_listing them (the image itself is staged server-side).
    try:
        batch_note = _attachment_batch_note(
            request, landlord, conversation_id, caption=message
        )
    except Exception as exc:
        from .attachment_services import AttachmentError

        if isinstance(exc, AttachmentError):
            return Response(
                {"detail": str(exc)},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        raise
    message = (
        f"{message}{batch_note}{_attachment_note(request, landlord)}"
        f"{_document_attachment_note(request, landlord)}"
    )

    result = run_turn(
        landlord, message, conversation_id, role="corporal", channel="web",
    )
    return _chat_response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def insights_view(request):
    """GET /api/rama/insights/?status=OPEN — Sergeant findings the FSA has
    analyzed, most recent first."""
    from .models import RamaInsight

    landlord = _landlord(request)
    qs = RamaInsight.objects.filter(landlord=landlord)
    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter.upper())
    return Response(
        {
            "insights": [
                {
                    "id": i.pk,
                    "kind": i.kind,
                    "severity": i.severity,
                    "facts": i.facts,
                    "analysis": i.analysis,
                    "status": i.status,
                    "created_at": i.created_at,
                }
                for i in qs[:100]
            ],
        },
    )


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def insight_detail_view(request, insight_id):
    """PATCH /api/rama/insights/<id>/ {status: ACKED|ACTIONED|DISMISSED}"""
    from .models import RamaInsight

    landlord = _landlord(request)
    insight = RamaInsight.objects.filter(pk=insight_id, landlord=landlord).first()
    if insight is None:
        return Response({"detail": "Not found."}, status=http_status.HTTP_404_NOT_FOUND)
    new_status = str(request.data.get("status") or "").strip().upper()
    valid = {c for c, _ in RamaInsight.Status.choices}
    if new_status not in valid:
        return Response(
            {"detail": f"status must be one of {sorted(valid)}."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    insight.status = new_status
    insight.save(update_fields=["status", "updated_at"])
    return Response({"id": insight.pk, "status": insight.status})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def holdings_view(request):
    """GET /api/rama/holdings/ — houses/buildings and their listings (what a
    bank-balance policy attaches to)."""
    from .domain_crud import list_holdings

    return Response(list_holdings(_landlord(request)))


def _document_payload(document, request=None):
    def file_url(field):
        if not field:
            return None
        url = field.url
        return request.build_absolute_uri(url) if request else url

    try:
        tag_rows = list(document.tags.all())
    except Exception:  # noqa: BLE001 — not prefetched / pre-migration
        tag_rows = []

    display = document.get_display_title()

    return {
        "id": str(document.pk),
        "status": document.status,
        "kind": document.kind,
        "kind_display": document.get_kind_display(),
        # Prefer semantic title over camera dump names in the UI.
        "title": document.title or display,
        "display_title": display,
        "issuer": document.issuer,
        "reference_number": document.reference_number,
        "document_date": str(document.document_date) if document.document_date else None,
        "due_date": str(document.due_date) if document.due_date else None,
        "amount": str(document.amount) if document.amount is not None else None,
        "currency": document.currency,
        "expense_category": document.expense_category,
        "payment_state": document.payment_state,
        "holding_id": str(document.holding_id) if document.holding_id else None,
        "holding_name": document.holding.name if document.holding_id else None,
        "property_id": document.property_id,
        "property_name": document.property.name if document.property_id else None,
        "portfolio_wide": document.portfolio_wide,
        "classification_confidence": str(document.classification_confidence),
        "match_confidence": str(document.match_confidence),
        "clarification_question": document.clarification_question,
        "clarification_answer": document.clarification_answer,
        "original_filename": document.original_filename,
        "canonical_filename": document.canonical_filename,
        "original_file": file_url(document.original_file),
        "archival_pdf": file_url(document.archival_pdf),
        "ledger_entry_id": str(document.ledger_entry_id) if document.ledger_entry_id else None,
        "failure_reason": document.failure_reason,
        "tags": [{"id": str(t.pk), "name": t.name, "slug": t.slug} for t in tag_rows],
        "created_at": document.created_at.isoformat(),
        "filed_at": document.filed_at.isoformat() if document.filed_at else None,
        "deleted_at": (
            document.deleted_at.isoformat()
            if getattr(document, "deleted_at", None)
            else None
        ),
        "in_trash": bool(getattr(document, "deleted_at", None)),
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def documents_view(request):
    """Upload or list the acting landlord's OCR-backed business records.

    GET supports pagination and library filters:
    ?page=&page_size=&q=&holding=&kind=&year=&status=&tag=&payment_state=&has_expense=
    """
    from .document_services import DocumentError
    from .document_services import ingest_document
    from .document_services import query_business_documents
    from .tasks import process_rama_document

    landlord = _landlord(request)
    if request.method == "GET":
        try:
            page = max(1, int(request.query_params.get("page") or "1"))
        except ValueError:
            page = 1
        try:
            page_size = min(
                100, max(1, int(request.query_params.get("page_size") or "25"))
            )
        except ValueError:
            page_size = 25
        try:
            result = query_business_documents(
                landlord,
                q=request.query_params.get("q") or "",
                holding_id=(
                    request.query_params.get("holding")
                    or request.query_params.get("holding_id")
                    or ""
                ),
                kind=request.query_params.get("kind") or "",
                year=request.query_params.get("year") or None,
                status=request.query_params.get("status") or "",
                tag=request.query_params.get("tag") or "",
                payment_state=request.query_params.get("payment_state") or "",
                has_expense=request.query_params.get("has_expense"),
                page=page,
                page_size=page_size,
            )
        except DocumentError as exc:
            return Response(
                {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "documents": [
                    _document_payload(row, request) for row in result["documents"]
                ],
                "pagination": result["pagination"],
            }
        )

    upload = request.FILES.get("file") or request.FILES.get("document")
    if not upload:
        return Response(
            {"detail": "A file is required."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    try:
        document, created = ingest_document(
            landlord=landlord, upload=upload, created_by=request.user,
        )
    except DocumentError as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    if created:
        process_rama_document.delay(str(document.pk))
    return Response(
        {**_document_payload(document, request), "duplicate": not created},
        status=http_status.HTTP_201_CREATED if created else http_status.HTTP_200_OK,
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def document_tags_view(request):
    """List landlord tags or create one (for autocomplete / library chips)."""
    from .document_services import DocumentError
    from .document_services import get_or_create_document_tag
    from .document_services import list_document_tags

    landlord = _landlord(request)
    if request.method == "GET":
        tags = list_document_tags(landlord)
        return Response(
            {
                "tags": [
                    {
                        "id": str(t.pk),
                        "name": t.name,
                        "slug": t.slug,
                        "document_count": getattr(t, "document_count", 0),
                    }
                    for t in tags
                ]
            }
        )
    name = (request.data or {}).get("name") or ""
    try:
        tag = get_or_create_document_tag(landlord, name)
    except DocumentError as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    return Response(
        {"id": str(tag.pk), "name": tag.name, "slug": tag.slug},
        status=http_status.HTTP_201_CREATED,
    )


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def document_detail_view(request, document_id):
    """Inspect OCR results, confirm/correct filing, or delete a business document."""
    from rentium.properties.models import Property
    from rentium.properties.models import PropertyHolding

    from .document_services import DocumentError
    from .document_services import DuplicateExpenseError
    from .document_services import delete_document
    from .document_services import file_document
    from .document_services import rename_document
    from .document_services import set_document_tags
    from .models import RamaDocument

    landlord = _landlord(request)
    document = (
        RamaDocument.objects.filter(pk=document_id, landlord=landlord)
        .select_related("holding", "property", "ledger_entry")
        .prefetch_related("tags")
        .first()
    )
    if document is None:
        return Response(
            {"detail": "Document not found."}, status=http_status.HTTP_404_NOT_FOUND,
        )
    if request.method == "DELETE":
        hard = str(request.query_params.get("hard") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            result = delete_document(
                landlord=landlord, document=document, hard=hard,
            )
        except DocumentError as exc:
            return Response(
                {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
            )
        return Response(result)
    if request.method == "GET":
        payload = _document_payload(document, request)
        payload["ocr_text"] = document.ocr_text
        payload["events"] = [
            {
                "kind": event.kind,
                "detail": event.detail,
                "created_at": event.created_at.isoformat(),
            }
            for event in document.events.all()
        ]
        return Response(payload)

    data = request.data or {}
    # Renaming is metadata-only. It must never re-file a record, post an
    # expense, or rewrite the preserved original/canonical archive filenames.
    if set(data.keys()) == {"title"}:
        try:
            rename_document(
                landlord=landlord,
                document=document,
                title=data.get("title"),
                actor=request.user,
            )
        except DocumentError as exc:
            return Response(
                {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
            )
        document.refresh_from_db()
        return Response(_document_payload(document, request))

    if "tags" in data:
        raw_tags = data.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [part.strip() for part in raw_tags.split(",") if part.strip()]
        try:
            set_document_tags(document, list(raw_tags), replace=True)
        except DocumentError as exc:
            return Response(
                {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
            )
        # Tag-only update (no re-file).
        if not any(
            key in data
            for key in (
                "holding_id",
                "property_id",
                "portfolio_wide",
                "kind",
                "title",
                "issuer",
                "amount",
                "expense_category",
                "payment_state",
                "clarification_answer",
            )
        ):
            document.refresh_from_db()
            return Response(_document_payload(document, request))

    holding = None
    property_obj = None
    if data.get("holding_id"):
        holding = PropertyHolding.objects.filter(
            pk=data["holding_id"], landlord=landlord,
        ).first()
        if holding is None:
            return Response(
                {"detail": "No such holding."}, status=http_status.HTTP_400_BAD_REQUEST,
            )
    if data.get("property_id"):
        property_obj = Property.objects.filter(
            pk=data["property_id"], landlord=landlord,
        ).first()
        if property_obj is None:
            return Response(
                {"detail": "No such listing."}, status=http_status.HTTP_400_BAD_REQUEST,
            )

    def parsed_date(key):
        raw = data.get(key)
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw))
        except ValueError as exc:
            raise DocumentError(f"{key} must be YYYY-MM-DD.") from exc

    try:
        file_document(
            document,
            actor=request.user,
            holding=holding,
            property=property_obj,
            kind=data.get("kind"),
            title=data.get("title"),
            amount=data.get("amount"),
            expense_category=data.get("expense_category"),
            payment_state=data.get("payment_state"),
            document_date=parsed_date("document_date"),
            due_date=parsed_date("due_date"),
            issuer=data.get("issuer"),
            reference_number=data.get("reference_number"),
            clarification_answer=str(data.get("clarification_answer") or ""),
            portfolio_wide=data.get("portfolio_wide", False),
            duplicate_resolution=str(data.get("duplicate_resolution") or ""),
        )
    except DuplicateExpenseError as exc:
        # 409, not 400: the request is well-formed, it just collides with an
        # expense already on the books. The candidates travel with it so the
        # UI can offer "attach to that one" rather than only "post anyway".
        return Response(
            {
                "detail": str(exc),
                "code": "DUPLICATE_EXPENSE",
                "candidates": exc.candidates,
                "resolutions": {
                    "link": "duplicate_resolution=link:<entry_id>",
                    "separate": "duplicate_resolution=new",
                },
            },
            status=http_status.HTTP_409_CONFLICT,
        )
    except (DocumentError, ValueError) as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    document.refresh_from_db()
    return Response(_document_payload(document, request))


def _get_landlord_document(landlord, document_id, *, include_trashed: bool = True):
    from .models import RamaDocument

    qs = (
        RamaDocument.objects.filter(pk=document_id, landlord=landlord)
        .select_related("holding", "property", "ledger_entry")
        .prefetch_related("tags")
    )
    if not include_trashed:
        qs = qs.filter(deleted_at__isnull=True)
    return qs.first()


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def document_download_view(request, document_id):
    """Authenticated download; private records are never exposed by a public URL."""
    document = _get_landlord_document(_landlord(request), document_id)
    if document is None:
        return Response(
            {"detail": "Document not found."}, status=http_status.HTTP_404_NOT_FOUND,
        )
    field = document.archival_pdf or document.original_file
    field.open("rb")
    return FileResponse(
        field,
        as_attachment=True,
        filename=document.canonical_filename or document.original_filename,
        content_type="application/pdf" if document.archival_pdf else document.media_type,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def document_reocr_view(request, document_id):
    """Re-run OCR after a FAILED pass (or to refresh text for search)."""
    from .document_services import DocumentError
    from .document_services import reocr_document
    from .models import RamaDocument

    landlord = _landlord(request)
    document = _get_landlord_document(landlord, document_id, include_trashed=False)
    if document is None:
        return Response(
            {"detail": "Document not found."}, status=http_status.HTTP_404_NOT_FOUND,
        )
    try:
        document = reocr_document(landlord=landlord, document=document)
    except DocumentError as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    document = (
        RamaDocument.objects.filter(pk=document.pk)
        .select_related("holding", "property", "ledger_entry")
        .prefetch_related("tags")
        .first()
    )
    payload = _document_payload(document, request)
    payload["ocr_text"] = (document.ocr_text or "")[:2000]
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def document_restore_view(request, document_id):
    """Restore a soft-deleted document from trash."""
    from .document_services import DocumentError
    from .document_services import restore_document

    landlord = _landlord(request)
    document = _get_landlord_document(landlord, document_id)
    if document is None:
        return Response(
            {"detail": "Document not found."}, status=http_status.HTTP_404_NOT_FOUND,
        )
    try:
        result = restore_document(landlord=landlord, document=document)
    except DocumentError as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    document.refresh_from_db()
    return Response({**result, **_document_payload(document, request)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def document_mark_paid_view(request, document_id):
    """Mark the linked ledger expense paid (and sync document payment_state)."""
    from .document_services import DocumentError
    from .document_services import mark_document_expense_paid

    landlord = _landlord(request)
    document = _get_landlord_document(landlord, document_id, include_trashed=False)
    if document is None:
        return Response(
            {"detail": "Document not found."}, status=http_status.HTTP_404_NOT_FOUND,
        )
    data = request.data or {}
    try:
        result = mark_document_expense_paid(
            landlord=landlord,
            document=document,
            paid_on=data.get("paid_on"),
        )
    except DocumentError as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    except Exception as exc:  # noqa: BLE001 — ledger validation
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    document.refresh_from_db()
    return Response({**result, **_document_payload(document, request)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def document_move_view(request, document_id):
    """Re-file a document (and reallocate its expense) to another holding."""
    from .document_services import DocumentError
    from .document_services import move_document_holding

    landlord = _landlord(request)
    document = _get_landlord_document(landlord, document_id, include_trashed=False)
    if document is None:
        return Response(
            {"detail": "Document not found."}, status=http_status.HTTP_404_NOT_FOUND,
        )
    data = request.data or {}
    portfolio_wide = bool(data.get("portfolio_wide"))
    holding_id = data.get("holding_id") or data.get("holding") or ""
    try:
        result = move_document_holding(
            landlord=landlord,
            document=document,
            holding=holding_id or None,
            portfolio_wide=portfolio_wide,
        )
    except DocumentError as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    document.refresh_from_db()
    return Response({**result, **_document_payload(document, request)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def documents_bulk_view(request):
    """Bulk trash / restore / tag / move / hard_delete for the document library.

    Body: {document_ids: [...], action, tag_names?, holding_id?, portfolio_wide?}
    """
    from .document_services import DocumentError
    from .document_services import bulk_document_action

    landlord = _landlord(request)
    data = request.data or {}
    try:
        result = bulk_document_action(
            landlord=landlord,
            document_ids=data.get("document_ids") or data.get("ids") or [],
            action=str(data.get("action") or ""),
            tag_names=data.get("tag_names") or data.get("tags") or None,
            holding_id=str(data.get("holding_id") or data.get("holding") or ""),
            portfolio_wide=bool(data.get("portfolio_wide")),
        )
    except DocumentError as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    return Response(result)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def bank_balances_view(request):
    """GET /api/rama/bank-balances/ — reported balances (+ staleness, drift).
    POST {holding_id?, label?, balance, as_of?} — a landlord-direct entry
    (no chat confirm needed for their own UI edit; chat/General-origin
    writes go through the guarded update_bank_balance tool instead)."""
    from .finance import balance_payload
    from .finance import list_bank_balances

    landlord = _landlord(request)
    if request.method == "GET":
        return Response(list_bank_balances(landlord))

    from datetime import date as _date
    from decimal import Decimal
    from decimal import InvalidOperation

    from rentium.ledger.models import PropertyBankBalance
    from rentium.properties.models import PropertyHolding

    data = request.data or {}
    holding = None
    holding_id = data.get("holding_id")
    if holding_id:
        holding = PropertyHolding.objects.filter(pk=holding_id, landlord=landlord).first()
        if holding is None:
            return Response(
                {"detail": "No such holding."}, status=http_status.HTTP_400_BAD_REQUEST,
            )
    try:
        balance = Decimal(str(data.get("balance")))
    except (InvalidOperation, TypeError):
        return Response(
            {"detail": "balance is required and must be a number."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    as_of_raw = str(data.get("as_of") or "").strip()
    try:
        as_of = _date.fromisoformat(as_of_raw) if as_of_raw else _date.today()
    except ValueError:
        return Response(
            {"detail": "as_of must be YYYY-MM-DD."}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    row, _created = PropertyBankBalance.objects.update_or_create(
        landlord=landlord, holding=holding,
        defaults={
            "label": str(data.get("label") or "Operating")[:100],
            "balance": balance,
            "as_of": as_of,
            "updated_via": PropertyBankBalance.Source.UI,
        },
    )
    return Response(balance_payload(row))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def general_chat_view(request):
    """POST /api/rama/general/chat/ — the General (chief of staff).

    Also the seam an external personal-assistant client uses later.
    """
    landlord = _landlord(request)
    cfg = get_landlord_config(landlord)
    if not cfg.enabled:
        return Response(
            {"detail": "RAMA is turned off for your account."},
            status=http_status.HTTP_403_FORBIDDEN,
        )
    role_cfg = get_role_config(landlord, "general")
    if not role_cfg.api_key:
        return Response(
            {
                "detail": (
                    "No API key available for the General's provider "
                    f"({role_cfg.provider}). Set one under Settings → RAMA."
                ),
            },
            status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    message, conversation_id, err = _validated_chat_input(request)
    if err is not None:
        return err
    try:
        message += _attachment_batch_note(
            request, landlord, conversation_id, caption=message
        )
    except Exception as exc:
        from .attachment_services import AttachmentError

        if isinstance(exc, AttachmentError):
            return Response(
                {"detail": str(exc)},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        raise

    result = run_turn(
        landlord, message, conversation_id, role="general", channel="web",
    )
    return _chat_response(result)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def treasurer_chat_view(request):
    """POST /api/rama/treasurer/chat/ — the Treasurer (finance head).

    Directly reachable as well as via the General's ask_treasurer, because a
    quick "what's my equity?" should not have to round-trip through a relay.
    Read-only over the domain: its tool list contains nothing that takes a
    `confirm` argument (asserted in test_treasurer), so no plan can originate
    here.
    """
    landlord = _landlord(request)
    cfg = get_landlord_config(landlord)
    if not cfg.enabled:
        return Response(
            {"detail": "RAMA is turned off for your account."},
            status=http_status.HTTP_403_FORBIDDEN,
        )
    role_cfg = get_role_config(landlord, "treasurer")
    if not role_cfg.api_key:
        return Response(
            {
                "detail": (
                    "No API key available for the Treasurer's provider "
                    f"({role_cfg.provider}). Set one under Settings → RAMA."
                ),
            },
            status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    message, conversation_id, err = _validated_chat_input(request)
    if err is not None:
        return err

    result = run_turn(
        landlord, message, conversation_id, role="treasurer", channel="web",
    )
    return _chat_response(result)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def constitution_view(request):
    """GET/POST /api/rama/constitution/ — the landlord's written policy.

    GET returns active sections + rules. POST amends one section (creates a
    new append-only version, origin LANDLORD — the landlord's own edits don't
    need the chat confirm gate; General-origin amendments go through the
    guarded amend_constitution tool instead).
    """
    from .constitution import amend
    from .constitution import parse_rule_changes
    from .constitution import section_payload
    from .models import RamaConstitutionSection

    landlord = _landlord(request)
    if request.method == "GET":
        return Response(section_payload(landlord))

    data = request.data or {}
    key = str(data.get("key") or "").strip().lower()
    if not key:
        return Response(
            {"detail": "key is required."}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    raw_changes = data.get("rule_changes")
    if isinstance(raw_changes, (list, dict)):
        import json as _json

        raw_changes = _json.dumps(raw_changes)
    changes, err_msg = parse_rule_changes(str(raw_changes or ""))
    if err_msg:
        return Response(
            {"detail": err_msg}, status=http_status.HTTP_400_BAD_REQUEST,
        )
    result = amend(
        landlord,
        key=key,
        title=str(data.get("title") or ""),
        body_md=str(data.get("body_md") or ""),
        rule_changes=changes,
        origin=RamaConstitutionSection.Origin.LANDLORD,
    )
    return Response({**result, **section_payload(landlord)})


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def capability_gaps_view(request):
    """GET  /api/rama/capability-gaps/?status=NEW — the backlog of things RAMA
    was asked for and couldn't do.
    PATCH /api/rama/capability-gaps/ {"id": ..., "status": "BUILT",
    "prioritised": true} — record a triage decision.

    RAMA already logged these; until now they were only readable from inside a
    chat, so "what have you been unable to do?" was invisible to anyone
    planning the work. Nothing here builds a capability — it records human
    decisions about a worklist.
    """
    from .models import RamaCapabilityGap

    landlord = _landlord(request)
    qs = RamaCapabilityGap.objects.filter(landlord=landlord)

    if request.method == "PATCH":
        gap_id = request.data.get("id")
        gap = qs.filter(pk=gap_id).first() if gap_id else None
        if gap is None:
            return Response({"error": "Unknown gap id."}, status=404)
        fields = ["updated_at"]
        new_status = str(request.data.get("status") or "").strip().upper()
        if new_status:
            valid = {c for c, _ in RamaCapabilityGap.Status.choices}
            if new_status not in valid:
                return Response(
                    {"error": f"status must be one of {sorted(valid)}."}, status=400,
                )
            gap.status = new_status
            fields.append("status")
        if "prioritised" in request.data:
            gap.prioritised = bool(request.data.get("prioritised"))
            fields.append("prioritised")
        gap.save(update_fields=fields)
        return Response(
            {
                "id": str(gap.pk),
                "status": gap.status,
                "prioritised": gap.prioritised,
            },
        )

    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter.upper())

    counts = {}
    for code, _label in RamaCapabilityGap.Status.choices:
        counts[code] = RamaCapabilityGap.objects.filter(
            landlord=landlord, status=code,
        ).count()

    return Response(
        {
            "counts": counts,
            "gaps": [
                {
                    "id": str(g.pk),
                    "request": g.request,
                    "detail": g.detail,
                    "status": g.status,
                    "prioritised": g.prioritised,
                    "created_at": g.created_at,
                    "updated_at": g.updated_at,
                }
                for g in qs[:200]
            ],
        },
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def auto_actions_view(request):
    """GET /api/rama/auto-actions/ — what RAMA did without asking.

    An unattended write the landlord can't see is indistinguishable from a bug,
    so this is the receipt drawer behind the "Done automatically · Undo" strip.
    Scoped to the acting landlord like every other RAMA surface.
    """
    from .autonomy import AUTO_UNDO_TTL
    from .models import RamaAutoAction

    landlord = _landlord(request)
    try:
        limit = max(1, min(int(request.query_params.get("limit") or 50), 200))
    except (TypeError, ValueError):
        limit = 50

    cutoff = timezone.now() - AUTO_UNDO_TTL
    rows = RamaAutoAction.objects.filter(landlord=landlord)[:limit]
    return Response(
        {
            "auto_actions": [
                {
                    "id": str(row.pk),
                    "tool": row.tool,
                    "target": row.target_label,
                    "status": row.status,
                    "conversation_id": str(row.conversation_id),
                    # Reported at read time rather than stored, so no job has to
                    # keep the flag honest.
                    "undoable": (
                        row.status == RamaAutoAction.Status.DONE
                        and bool(row.undo_tool)
                        and row.created_at >= cutoff
                    ),
                    "created_at": row.created_at.isoformat(),
                    "undone_at": row.undone_at.isoformat() if row.undone_at else None,
                }
                for row in rows
            ],
        },
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def auto_action_undo_view(request, action_id):
    """POST /api/rama/auto-actions/<id>/undo/ — reverse one auto-executed action.

    The inverse runs through the normal plan runner, so this endpoint gains no
    privilege the chat path doesn't already have.
    """
    from .autonomy import undo_action
    from .models import RamaAudit
    from .models import RamaAutoAction

    landlord = _landlord(request)
    action = RamaAutoAction.objects.filter(landlord=landlord, pk=action_id).first()
    if action is None:
        return Response({"error": "Unknown action."}, status=404)

    def _audit(content):
        RamaAudit.objects.create(
            landlord=landlord,
            conversation_id=action.conversation_id,
            kind=RamaAudit.Kind.TOOL_CALL,
            content={**content, "undo_of": str(action.pk), "via": "api"},
        )

    outcome = undo_action(action, landlord, audit=_audit)
    if outcome.get("error"):
        return Response(outcome, status=http_status.HTTP_400_BAD_REQUEST)
    return Response(outcome)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def memory_view(request):
    """GET /api/rama/memory/ — the durable preferences RAMA holds for this
    landlord. Also serves as the data-portability export for them."""
    from .memory import payload

    return Response(payload(_landlord(request), request.query_params.get("q") or ""))


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def memory_delete_view(request, memory_id):
    """DELETE /api/rama/memory/<id>/ — erase one memory outright.

    Genuine erasure, not a status flag: a privacy request has to actually
    remove the text. The audit row records the key and the fact of deletion and
    deliberately NEVER the body — otherwise "erasing" a memory would just move
    the personal data into the append-only audit trail, which is worse than not
    deleting at all. What survives is proof that something was removed and when.
    """
    from .models import RamaAudit
    from .models import RamaMemory

    landlord = _landlord(request)
    row = RamaMemory.objects.filter(landlord=landlord, pk=memory_id).first()
    if row is None:
        return Response({"error": "Unknown memory."}, status=404)

    key = row.key
    row.delete()
    RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=row.origin_conversation or uuid.uuid4(),
        kind=RamaAudit.Kind.TOOL_CALL,
        content={"tool": "_memory_erased", "arguments": {"key": key}, "result": {"deleted": True}},
    )
    return Response({"deleted": True, "subject": key})


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def treasurer_view(request):
    """GET/PATCH /api/rama/treasurer/ — the Treasurer's own settings page.

    Four things a landlord needs to see about a background finance agent:
    what it is allowed to know about them (consent), what it is still waiting
    on (open requests), what it has concluded (deliberations), and where the
    portfolio data it reasons over is missing.

    PATCH only ever writes the consent gate and the personal fields behind it.
    Holding financials, valuations and mortgages are written by the General
    through the normal confirm-previewed tools — deliberately, because the
    agent that concludes "your equity looks strong" must not be the one that
    types in the valuation.
    """
    from django.utils import timezone

    from .models import LandlordFinancialProfile
    from .models import RamaDeliberation

    landlord = _landlord(request)
    profile, _ = LandlordFinancialProfile.objects.get_or_create(landlord=landlord)

    if request.method == "PATCH":
        data = request.data or {}
        if "consented" in data:
            # Withdrawing consent takes effect immediately and blanks nothing:
            # the fields stay, they simply stop being readable. Deleting them
            # would make re-consenting mean re-typing everything.
            profile.consented_at = timezone.now() if data["consented"] else None
        for field in (
            "occupation",
            "employment_income_band",
            "other_income_band",
            "filing_situation",
            "tax_province",
        ):
            if field in data:
                setattr(profile, field, data[field] or "")
        if "self_reported_marginal_rate" in data:
            raw = data["self_reported_marginal_rate"]
            if raw in (None, ""):
                profile.self_reported_marginal_rate = None
            else:
                try:
                    profile.self_reported_marginal_rate = Decimal(str(raw))
                except (InvalidOperation, TypeError):
                    return Response(
                        {"error": "invalid", "detail": "Marginal rate must be a number."},
                        status=http_status.HTTP_400_BAD_REQUEST,
                    )
        try:
            profile.full_clean()
        except DjangoValidationError as exc:
            return Response(
                {"error": "invalid", "detail": exc.message_dict},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        profile.save()

    from .deliberation import open_requests

    deliberations = (
        RamaDeliberation.objects.filter(landlord=landlord)
        .order_by("-created_at")
        .select_related("holding")[:20]
    )
    return Response(
        {
            "profile": {
                "consented": profile.usable,
                "consent_scope": profile.consent_scope,
                "occupation": profile.occupation,
                "employment_income_band": profile.employment_income_band,
                "other_income_band": profile.other_income_band,
                "filing_situation": profile.filing_situation,
                "tax_province": profile.tax_province,
                "self_reported_marginal_rate": (
                    str(profile.self_reported_marginal_rate)
                    if profile.self_reported_marginal_rate is not None
                    else None
                ),
            },
            "choices": {
                "income_bands": [
                    {"value": v, "label": label}
                    for v, label in LandlordFinancialProfile.IncomeBand.choices
                ],
                "filing_situations": [
                    {"value": v, "label": label}
                    for v, label in LandlordFinancialProfile.Filing.choices
                ],
            },
            "requests": [
                {
                    "id": str(r.pk),
                    "question": r.question,
                    "why_it_matters": r.why_it_matters,
                    "blocking": r.blocking,
                    "status": r.status,
                    "created_at": r.created_at.isoformat(),
                }
                for r in open_requests(landlord)
            ],
            "deliberations": [
                {
                    "id": str(d.pk),
                    "topic": d.topic,
                    "question": d.question,
                    "status": d.status,
                    "trigger": d.trigger,
                    "holding": d.holding.name if d.holding_id else None,
                    "created_at": d.created_at.isoformat(),
                }
                for d in deliberations
            ],
            "data_gaps": _treasurer_data_gaps(landlord),
        },
    )


def _treasurer_data_gaps(landlord) -> list[dict]:
    """What the Treasurer cannot work out yet, and why.

    Stated as concrete missing facts rather than a percentage, because "62%
    complete" tells a landlord nothing about what to go and do.
    """
    from rentium.ledger.models import HoldingFinancials
    from rentium.ledger.models import HoldingMortgage
    from rentium.properties.models import PropertyHolding

    gaps = []
    for holding in PropertyHolding.objects.filter(landlord=landlord):
        missing = []
        financials = HoldingFinancials.objects.filter(holding=holding).first()
        if financials is None or not financials.purchase_price:
            missing.append("what you paid for it")
        if financials is None or not financials.year_built:
            missing.append("the year it was built")
        if not holding.valuations.exists():
            missing.append("a recent valuation")
        if not HoldingMortgage.objects.filter(
            holding=holding, status=HoldingMortgage.Status.ACTIVE,
        ).exists():
            missing.append("the mortgage on it")
        if missing:
            gaps.append({"holding": holding.name, "missing": missing})
    return gaps
