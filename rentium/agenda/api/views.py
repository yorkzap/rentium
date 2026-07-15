"""
Unified calendar feed for landlords.

GET /api/agenda/?start=YYYY-MM-DD&end=YYYY-MM-DD[&property=<id>]
  -> merges, into one date-sorted list:
       * custom AgendaEvents
       * lease start / end (renewal cues)
       * upcoming rent charges that are still owed
       * scheduled work orders
  Each item: {date, type, title, subtitle, ref_id, url}

POST/PATCH/DELETE /api/agenda/events/  -> CRUD for custom entries only.
"""

from datetime import date
from datetime import datetime

from rest_framework import viewsets
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import AgendaEvent
from .serializers import AgendaEventSerializer


def _d(s, default):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


class AgendaEventViewSet(viewsets.ModelViewSet):
    serializer_class = AgendaEventSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        u = self.request.user
        if not hasattr(u, "landlord_profile"):
            return AgendaEvent.objects.none()
        return AgendaEvent.objects.filter(owner=u.landlord_profile)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user.landlord_profile)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def agenda_feed(request):
    u = request.user
    if not hasattr(u, "landlord_profile"):
        raise PermissionDenied("Landlords only.")
    landlord = u.landlord_profile

    today = date.today()
    start = _d(request.query_params.get("start"), today.replace(day=1))
    end = _d(
        request.query_params.get("end"),
        date(today.year + (1 if today.month == 12 else 0), (today.month % 12) + 1, 1),
    )
    prop = request.query_params.get("property")

    items = []

    # custom entries
    custom = AgendaEvent.objects.filter(
        owner=landlord, start_date__gte=start, start_date__lte=end
    )
    if prop:
        custom = custom.filter(property_id=prop)
    for e in custom:
        items.append(
            {
                "date": str(e.start_date),
                "type": e.kind.lower(),
                "title": e.title,
                "subtitle": e.notes[:80],
                "ref_id": str(e.id),
                "url": "/dashboard/calendar",
            }
        )

    # leases: start / end
    from rentium.leases.models import Lease

    leases = Lease.objects.filter(landlord=landlord)
    if prop:
        leases = leases.filter(property_id=prop)
    for lease in leases:
        if lease.start_date and start <= lease.start_date <= end:
            items.append(
                {
                    "date": str(lease.start_date),
                    "type": "lease_start",
                    "title": "Lease starts",
                    "subtitle": getattr(lease, "lease_number", ""),
                    "ref_id": str(lease.pk),
                    "url": f"/dashboard/leases/{lease.pk}",
                }
            )
        if lease.end_date and start <= lease.end_date <= end:
            items.append(
                {
                    "date": str(lease.end_date),
                    "type": "lease_end",
                    "title": "Lease ends",
                    "subtitle": getattr(lease, "lease_number", ""),
                    "ref_id": str(lease.pk),
                    "url": f"/dashboard/leases/{lease.pk}",
                }
            )

    # rent + other charges still owed, due in range
    from rentium.ledger.models import INCOME_CHARGE_TYPES
    from rentium.ledger.models import LedgerEntry

    charges = LedgerEntry.objects.with_settlement().filter(
        landlord=landlord,
        entry_type__in=INCOME_CHARGE_TYPES,
        reversed_by__isnull=True,
        due_date__gte=start,
        due_date__lte=end,
        outstanding__gt=0,
    )
    if prop:
        charges = charges.filter(property_id=prop)
    for c in charges.select_related("tenant"):
        items.append(
            {
                "date": str(c.due_date),
                "type": "charge_due",
                "title": f"{c.get_entry_type_display()} due",
                "subtitle": f"${c.outstanding} — {c.tenant_name or ''}".strip(),
                "ref_id": str(c.id),
                "url": "/dashboard/financial",
            }
        )

    # scheduled work orders
    from rentium.maintenance.models import WorkOrder

    wos = WorkOrder.objects.filter(
        property__landlord=landlord,
        scheduled_date__gte=start,
        scheduled_date__lte=end,
    ).exclude(status__in=["COMPLETED", "CANCELLED"])
    if prop:
        wos = wos.filter(property_id=prop)
    for wo in wos.select_related("property"):
        items.append(
            {
                "date": str(wo.scheduled_date),
                "type": "work_order",
                "title": f"Work: {wo.title}",
                "subtitle": wo.property.name,
                "ref_id": str(wo.id),
                "url": "/dashboard/maintenance",
            }
        )

    items.sort(key=lambda x: x["date"])
    return Response({"start": str(start), "end": str(end), "items": items})
