"""Thin authenticated adapters for the shared money and reconciliation services."""

from datetime import date
from functools import wraps

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .. import reconciliation as reconcile
from ..models import BankStatement, BankTransaction, ExpenseCategory
from ..money import overview
from ..services import LedgerError
from .import_views import _landlord


def errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (
            LedgerError,
            ValueError,
            TypeError,
            KeyError,
            DjangoValidationError,
        ) as exc:
            return Response({"detail": str(exc)}, status=400)
        except (BankTransaction.DoesNotExist, BankStatement.DoesNotExist) as exc:
            raise Http404 from exc

    return wrapped


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@errors
def money_overview(request):
    today = timezone.localdate()
    start = date.fromisoformat(
        request.query_params.get("start", today.replace(day=1).isoformat())
    )
    end = date.fromisoformat(
        request.query_params.get(
            "end",
            date(today.year + (today.month // 12), today.month % 12 + 1, 1).isoformat(),
        )
    )
    scope = {
        f"{key}_id": request.query_params.get(key) or None
        for key in ("property", "holding", "unit")
    }
    return Response(overview(_landlord(request), start, end, **scope))


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@errors
def statements(request):
    landlord = _landlord(request)
    if request.method == "POST":
        file = request.FILES.get("file")
        if not file:
            raise ValueError("Choose a CSV or XLSX statement.")
        if file.size > 10 * 1024 * 1024:
            raise ValueError("Choose a statement smaller than 10 MB.")
        payload, created = reconcile.upload(
            landlord,
            file.read(),
            file.name,
            request.data.get("account", ""),
            actor=request.user,
            worksheet=request.data.get("worksheet", ""),
        )
        return Response(payload, status=201 if created else 200)
    return Response(
        {
            "statements": [
                {
                    "id": str(s.batch_id),
                    "account": s.account,
                    "filename": s.batch.source_filename,
                    "status": s.batch.status,
                    "pending": s.transactions.filter(result={}).count(),
                    "created_at": s.batch.created_at,
                }
                for s in BankStatement.objects.filter(batch__landlord=landlord)
                .select_related("batch")
                .order_by("-batch__created_at")
            ]
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@transaction.atomic
@errors
def statement_detail(request, batch_id):
    landlord = _landlord(request)
    if request.method == "GET":
        # Status polling must not wait on the preparation worker's owner lock.
        statement = BankStatement.objects.select_related("batch").get(
            batch_id=batch_id, batch__landlord=landlord
        )
        return Response(reconcile.detail(statement))
    statement = reconcile.locked_statement(landlord, batch_id)
    action = request.data.get("action")
    if action == "columns":
        from ..statement_files import read_statement, guess_mapping

        with statement.batch.source_file.open("rb") as source:
            headers, _, sheets, selected = read_statement(
                source.read(),
                statement.batch.source_filename,
                request.data.get("worksheet", ""),
            )
        return Response(
            {
                "headers": headers,
                "worksheets": sheets,
                "worksheet": selected,
                "mapping": guess_mapping(headers),
            }
        )
    if action == "mapping":
        return Response(
            reconcile.prepare_statement(
                landlord,
                batch_id,
                request.data.get("mapping"),
                request.data.get("date_format", "%Y-%m-%d"),
                request.data.get("worksheet", ""),
            )
        )
    if action == "decision":
        return Response(
            reconcile.decide(
                landlord,
                batch_id,
                request.data.get("row_id"),
                request.data.get("decision"),
            )
        )
    if action == "preview":
        ids = request.data.get("row_ids")
        if not isinstance(ids, list) or any(type(ident) is not int for ident in ids):
            raise ValueError("Select statement rows to preview.")
        return Response(reconcile.preview(landlord, batch_id, ids))
    if action == "confirm":
        return Response(
            reconcile.commit(
                landlord,
                batch_id,
                request.data.get("preview_id", ""),
                actor=request.user,
            )
        )
    if action == "cancel":
        reconcile.invalidate(statement)
        return Response(reconcile.detail(statement))
    raise ValueError("Choose mapping, decision, preview, confirm, or cancel.")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@transaction.atomic
@errors
def review_options(request):
    from rentium.properties.models import Property, PropertyHolding, PropertyUnit
    from rentium.rama.models import RamaDocument

    landlord = _landlord(request)
    # Exact ID lookup is always owner scoped; no broad document contents leave
    # this endpoint, just enough reviewed facts to attach source evidence.
    documents = RamaDocument.objects.filter(
        landlord=landlord,
        amount__isnull=False,
        deleted_at__isnull=True,
        status__in=[
            RamaDocument.Status.NEEDS_REVIEW,
            RamaDocument.Status.READY,
            RamaDocument.Status.FILED,
        ],
    ).order_by("-created_at")[:250]
    entries = []
    if request.query_params.get("batch"):
        entries = reconcile.candidates(
            BankStatement.objects.select_related("batch").get(
                batch_id=request.query_params["batch"], batch__landlord=landlord
            )
        )
    return Response(
        {
            "holdings": list(
                PropertyHolding.objects.filter(landlord=landlord).values("id", "name")
            ),
            "units": list(
                PropertyUnit.objects.filter(landlord=landlord).values(
                    "id", "name", "holding_id"
                )
            ),
            "properties": list(
                Property.objects.filter(landlord=landlord).values(
                    "id", "name", "holding_id", "unit_id"
                )
            ),
            "categories": [
                {"id": key, "name": label} for key, label in ExpenseCategory.choices
            ],
            "documents": [
                {
                    "id": str(d.pk),
                    "name": d.title or d.original_filename,
                    "amount": str(d.amount),
                    "entry_id": str(d.ledger_entry_id) if d.ledger_entry_id else None,
                }
                for d in documents
            ],
            "entries": entries,
        }
    )
