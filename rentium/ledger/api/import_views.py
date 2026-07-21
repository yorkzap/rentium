"""
Historical import API: upload → confirm column mapping → review/edit
staged rows → commit. See ledger/import_services.py for the actual parsing,
validation, and commit logic — these views are thin request/response glue.
"""

from __future__ import annotations

from rest_framework import status as http_status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .. import import_services
from ..models import ImportBatch, StagedLedgerEntry


def _landlord(request):
    if not hasattr(request.user, "landlord_profile"):
        raise PermissionDenied("Landlords only.")
    return request.user.landlord_profile


def _batch_or_404(request, batch_id):
    return ImportBatch.objects.filter(
        pk=batch_id, landlord=_landlord(request)
    ).first()


def _row_payload(row: StagedLedgerEntry) -> dict:
    return {
        "id": str(row.pk),
        "row_number": row.row_number,
        "entry_type": row.entry_type,
        "amount": str(row.amount) if row.amount is not None else None,
        "due_date": row.due_date,
        "effective_date": row.effective_date,
        "paid_on": row.paid_on,
        "property_id": str(row.property_id) if row.property_id else None,
        "property_name": row.property.name if row.property_id else None,
        "lease_id": str(row.lease_id) if row.lease_id else None,
        "tenant_id": str(row.tenant_id) if row.tenant_id else None,
        "category": row.category,
        "vendor": row.vendor,
        "description": row.description,
        "payment_method": row.payment_method,
        "settles_row_id": str(row.settles_row_id) if row.settles_row_id else None,
        "issues": row.issues,
        "committed": row.committed_entry_id is not None,
        "raw": row.raw,
    }


def _batch_payload(batch: ImportBatch) -> dict:
    return {
        "id": str(batch.pk),
        "label": batch.label,
        "source_filename": batch.source_filename,
        "status": batch.status,
        "column_map": batch.column_map,
        "created_at": batch.created_at,
        "committed_at": batch.committed_at,
        "row_count": batch.rows.count(),
        "issue_count": sum(1 for r in batch.rows.all() if r.issues),
    }


@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser])
@permission_classes([IsAuthenticated])
def batches_view(request):
    """GET list this landlord's import batches. POST (multipart: file,
    label?) creates a DRAFT batch, stores the file, and returns headers +
    a best-effort column-mapping guess — nothing is staged until the
    mapping is confirmed via apply_mapping_view."""
    landlord = _landlord(request)
    if request.method == "GET":
        return Response(
            {"batches": [_batch_payload(b) for b in ImportBatch.objects.filter(landlord=landlord)]}
        )

    upload = request.FILES.get("file")
    if not upload:
        return Response(
            {"detail": "file is required (CSV)."}, status=http_status.HTTP_400_BAD_REQUEST
        )
    content = upload.read()
    try:
        headers, _rows = import_services.read_csv_rows(content)
    except Exception as exc:  # noqa: BLE001
        return Response(
            {"detail": f"Couldn't read that file as CSV: {exc}"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if not headers:
        return Response(
            {"detail": "No columns found — is this a CSV file?"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    batch = ImportBatch.objects.create(
        landlord=landlord,
        label=str(request.data.get("label") or "")[:200],
        source_filename=upload.name[:255],
        created_by=request.user,
    )
    batch.source_file.save(upload.name, upload, save=True)
    return Response(
        {
            **_batch_payload(batch),
            "headers": headers,
            "guessed_map": import_services.guess_column_map(headers),
            "target_fields": list(import_services.TARGET_FIELDS),
        },
        status=http_status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def apply_mapping_view(request, batch_id):
    """POST {column_map: {csv_header: target_field}} — (re)stages every row
    from the uploaded file using this mapping. Safe to call again after
    fixing the mapping; previously staged rows are replaced."""
    batch = _batch_or_404(request, batch_id)
    if batch is None:
        return Response({"detail": "Not found."}, status=http_status.HTTP_404_NOT_FOUND)
    if batch.status != ImportBatch.Status.DRAFT:
        return Response(
            {"detail": f"Batch is {batch.status}, not editable."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    column_map = request.data.get("column_map")
    if not isinstance(column_map, dict):
        return Response(
            {"detail": "column_map must be an object."}, status=http_status.HTTP_400_BAD_REQUEST
        )
    if not batch.source_file:
        return Response(
            {"detail": "No source file on this batch."}, status=http_status.HTTP_400_BAD_REQUEST
        )
    batch.source_file.seek(0)
    headers, rows = import_services.read_csv_rows(batch.source_file.read())
    import_services.stage_rows(batch, headers, rows, column_map)
    batch.column_map = column_map
    batch.save(update_fields=["column_map"])
    return Response(_batch_payload(batch))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def batch_rows_view(request, batch_id):
    """GET the staged rows for review (capped at 1000 — imports are
    landlord-sized spreadsheets, not data-warehouse loads)."""
    batch = _batch_or_404(request, batch_id)
    if batch is None:
        return Response({"detail": "Not found."}, status=http_status.HTTP_404_NOT_FOUND)
    rows = batch.rows.select_related("property", "settles_row").order_by("row_number")[:1000]
    return Response({"batch": _batch_payload(batch), "rows": [_row_payload(r) for r in rows]})


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def row_detail_view(request, batch_id, row_id):
    """PATCH edits a staged row (mutate before commit) and re-validates it.
    DELETE removes a bad row from staging entirely."""
    from rentium.properties.models import Property

    batch = _batch_or_404(request, batch_id)
    if batch is None:
        return Response({"detail": "Not found."}, status=http_status.HTTP_404_NOT_FOUND)
    if batch.status != ImportBatch.Status.DRAFT:
        return Response(
            {"detail": f"Batch is {batch.status}, not editable."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    row = batch.rows.filter(pk=row_id).first()
    if row is None:
        return Response({"detail": "Not found."}, status=http_status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        row.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)

    from decimal import Decimal, InvalidOperation

    from .. import import_services as _isvc

    data = request.data or {}
    if "amount" in data:
        try:
            row.amount = Decimal(str(data["amount"])) if data["amount"] not in (None, "") else None
        except InvalidOperation:
            return Response(
                {"detail": "amount must be a number."}, status=http_status.HTTP_400_BAD_REQUEST
            )
    for f in ("due_date", "effective_date", "paid_on"):
        if f in data:
            raw = data[f]
            setattr(row, f, _isvc._parse_date(raw) if raw else None)
    for f in ("entry_type", "category", "vendor", "description", "payment_method"):
        if f in data:
            setattr(row, f, data[f] or "")
    if "property_id" in data:
        row.property = (
            Property.objects.filter(pk=data["property_id"], landlord=batch.landlord).first()
            if data["property_id"]
            else None
        )
    if "settles_row_id" in data:
        row.settles_row = (
            batch.rows.filter(pk=data["settles_row_id"]).first()
            if data["settles_row_id"]
            else None
        )
    row.issues = import_services.validate_row(row)
    row.save()
    return Response(_row_payload(row))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def commit_batch_view(request, batch_id):
    """POST — commit every issue-free row. Rows with issues stay staged;
    the batch only flips to COMMITTED once none remain."""
    batch = _batch_or_404(request, batch_id)
    if batch is None:
        return Response({"detail": "Not found."}, status=http_status.HTTP_404_NOT_FOUND)
    report = import_services.commit_batch(batch, created_by=request.user)
    return Response(report)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def discard_batch_view(request, batch_id):
    """POST — discard a batch. Already-committed rows (real ledger entries)
    are NOT touched or removed; only remaining uncommitted staged rows stop
    being editable/committable."""
    batch = _batch_or_404(request, batch_id)
    if batch is None:
        return Response({"detail": "Not found."}, status=http_status.HTTP_404_NOT_FOUND)
    batch.status = ImportBatch.Status.DISCARDED
    batch.save(update_fields=["status"])
    return Response(_batch_payload(batch))
