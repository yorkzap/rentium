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
from django.http import FileResponse

from rest_framework import status as http_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import RamaPreferences
from .providers import PROVIDERS
from .runtime import (
    MODEL_CATALOG,
    get_landlord_config,
    get_role_config,
    platform_api_key,
)
from .service import MAX_MESSAGE_CHARS, PENDING_ACTION_TTL_SECONDS, run_turn
from .union import state_of_the_union

__all__ = [
    "chat_view",
    "upload_view",
    "general_chat_view",
    "constitution_view",
    "insights_view",
    "insight_detail_view",
    "holdings_view",
    "bank_balances_view",
    "config_view",
    "settings_view",
    "union_view",
    "PENDING_ACTION_TTL_SECONDS",  # re-exported for tests/back-compat
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
        }
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
        }
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
    for role in ("general", "fsa"):
        role_err = _apply_role_prefs(prefs, data, role)
        if role_err:
            return Response(
                {"detail": role_err}, status=http_status.HTTP_400_BAD_REQUEST
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
        }
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
        {"upload_id": str(upload.pk)}, status=http_status.HTTP_201_CREATED
    )


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
            landlord=landlord, used_at__isnull=True, pk__in=ids
        ).values_list("pk", flat=True)
    ]
    if not valid:
        return ""
    tags = " ".join(f"[The landlord attached a photo, upload_id={u}]" for u in valid)
    return (
        f"\n\n{tags}\nFirst determine intent from the landlord's words. If this is "
        "a property marketing/inspection photo, use attach_photo_to_listing. If "
        "they call it a document, mail, letter, receipt, invoice, notice, statement, "
        "or paperwork, use catalog_business_document with upload_id so it enters "
        "OCR/archive storage. For an address/property overall, set scope_query to "
        "that address and NEVER ask them to choose a child listing."
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
                "Enable it under Account → RAMA."
            },
            status=http_status.HTTP_403_FORBIDDEN,
        )
    if not cfg.is_configured():
        return Response(
            {
                "detail": (
                    "Add your API key under Account → RAMA (e.g. an xAI Grok key), "
                    "then try again."
                )
            },
            status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    message, conversation_id, err = _validated_chat_input(request)
    if err is not None:
        return err

    # If the landlord attached photo(s) in the chat, tell the model so it can
    # attach_photo_to_listing them (the image itself is staged server-side).
    message = (
        f"{message}{_attachment_note(request, landlord)}"
        f"{_document_attachment_note(request, landlord)}"
    )

    result = run_turn(
        landlord, message, conversation_id, role="corporal", channel="web"
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
            ]
        }
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

    return {
        "id": str(document.pk),
        "status": document.status,
        "kind": document.kind,
        "kind_display": document.get_kind_display(),
        "title": document.title,
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
        "created_at": document.created_at.isoformat(),
        "filed_at": document.filed_at.isoformat() if document.filed_at else None,
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def documents_view(request):
    """Upload or list the acting landlord's OCR-backed business records."""
    from .document_services import DocumentError, ingest_document
    from .models import RamaDocument
    from .tasks import process_rama_document

    landlord = _landlord(request)
    if request.method == "GET":
        queryset = (
            RamaDocument.objects.filter(landlord=landlord)
            .select_related("holding", "property", "ledger_entry")[:200]
        )
        status_filter = str(request.query_params.get("status") or "").upper()
        if status_filter:
            queryset = (
                RamaDocument.objects.filter(
                    landlord=landlord, status=status_filter
                )
                .select_related("holding", "property", "ledger_entry")[:200]
            )
        return Response({"documents": [_document_payload(row, request) for row in queryset]})

    upload = request.FILES.get("file") or request.FILES.get("document")
    if not upload:
        return Response(
            {"detail": "A file is required."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    try:
        document, created = ingest_document(
            landlord=landlord, upload=upload, created_by=request.user
        )
    except DocumentError as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST
        )
    if created:
        process_rama_document.delay(str(document.pk))
    return Response(
        {**_document_payload(document, request), "duplicate": not created},
        status=http_status.HTTP_201_CREATED if created else http_status.HTTP_200_OK,
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def document_detail_view(request, document_id):
    """Inspect OCR results or confirm/correct the proposed filing."""
    from rentium.properties.models import Property, PropertyHolding

    from .document_services import DocumentError, file_document
    from .models import RamaDocument

    landlord = _landlord(request)
    document = (
        RamaDocument.objects.filter(pk=document_id, landlord=landlord)
        .select_related("holding", "property", "ledger_entry")
        .first()
    )
    if document is None:
        return Response(
            {"detail": "Document not found."}, status=http_status.HTTP_404_NOT_FOUND
        )
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
    holding = None
    property_obj = None
    if data.get("holding_id"):
        holding = PropertyHolding.objects.filter(
            pk=data["holding_id"], landlord=landlord
        ).first()
        if holding is None:
            return Response(
                {"detail": "No such holding."}, status=http_status.HTTP_400_BAD_REQUEST
            )
    if data.get("property_id"):
        property_obj = Property.objects.filter(
            pk=data["property_id"], landlord=landlord
        ).first()
        if property_obj is None:
            return Response(
                {"detail": "No such listing."}, status=http_status.HTTP_400_BAD_REQUEST
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
        )
    except (DocumentError, ValueError) as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST
        )
    document.refresh_from_db()
    return Response(_document_payload(document, request))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def document_download_view(request, document_id):
    """Authenticated download; private records are never exposed by a public URL."""
    from .models import RamaDocument

    document = RamaDocument.objects.filter(
        pk=document_id, landlord=_landlord(request)
    ).first()
    if document is None:
        return Response(
            {"detail": "Document not found."}, status=http_status.HTTP_404_NOT_FOUND
        )
    field = document.archival_pdf or document.original_file
    field.open("rb")
    return FileResponse(
        field,
        as_attachment=True,
        filename=document.canonical_filename or document.original_filename,
        content_type="application/pdf" if document.archival_pdf else document.media_type,
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def bank_balances_view(request):
    """GET /api/rama/bank-balances/ — reported balances (+ staleness, drift).
    POST {holding_id?, label?, balance, as_of?} — a landlord-direct entry
    (no chat confirm needed for their own UI edit; chat/General-origin
    writes go through the guarded update_bank_balance tool instead)."""
    from .finance import balance_payload, list_bank_balances

    landlord = _landlord(request)
    if request.method == "GET":
        return Response(list_bank_balances(landlord))

    from datetime import date as _date
    from decimal import Decimal, InvalidOperation

    from rentium.ledger.models import PropertyBankBalance
    from rentium.properties.models import PropertyHolding

    data = request.data or {}
    holding = None
    holding_id = data.get("holding_id")
    if holding_id:
        holding = PropertyHolding.objects.filter(pk=holding_id, landlord=landlord).first()
        if holding is None:
            return Response(
                {"detail": "No such holding."}, status=http_status.HTTP_400_BAD_REQUEST
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
            {"detail": "as_of must be YYYY-MM-DD."}, status=http_status.HTTP_400_BAD_REQUEST
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
                )
            },
            status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    message, conversation_id, err = _validated_chat_input(request)
    if err is not None:
        return err

    result = run_turn(
        landlord, message, conversation_id, role="general", channel="web"
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
    from .constitution import amend, parse_rule_changes, section_payload
    from .models import RamaConstitutionSection

    landlord = _landlord(request)
    if request.method == "GET":
        return Response(section_payload(landlord))

    data = request.data or {}
    key = str(data.get("key") or "").strip().lower()
    if not key:
        return Response(
            {"detail": "key is required."}, status=http_status.HTTP_400_BAD_REQUEST
        )
    raw_changes = data.get("rule_changes")
    if isinstance(raw_changes, (list, dict)):
        import json as _json

        raw_changes = _json.dumps(raw_changes)
    changes, err_msg = parse_rule_changes(str(raw_changes or ""))
    if err_msg:
        return Response(
            {"detail": err_msg}, status=http_status.HTTP_400_BAD_REQUEST
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
                    {"error": f"status must be one of {sorted(valid)}."}, status=400
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
            }
        )

    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter.upper())

    counts = {}
    for code, _label in RamaCapabilityGap.Status.choices:
        counts[code] = RamaCapabilityGap.objects.filter(
            landlord=landlord, status=code
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
        }
    )
