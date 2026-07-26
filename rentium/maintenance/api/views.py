# rentium/maintenance/api/views.py
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser
from rest_framework.parsers import JSONParser
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rentium.leases.models import Lease
from rentium.properties.areas import areas_for_tenant_room
from rentium.properties.models import PropertyArea
from rentium.properties.models import Property

from ..models import WorkOrder
from ..models import WorkOrderComment
from ..models import WorkOrderImage
from .serializers import AreaSerializer
from .serializers import WorkOrderCommentSerializer
from .serializers import WorkOrderImageSerializer
from .serializers import WorkOrderSerializer


class WorkOrderViewSet(viewsets.ModelViewSet):
    """
    /api/maintenance/work-orders/

    Landlords: full management on their properties' work orders.
    Tenants: create/see work orders scoped to their space — their own
    tickets, plus any ticket on their room, on shared common areas, on
    areas exclusive to their room, or property-wide (no area set) for a
    property they actively rent. Status changes go through /transition/
    only, so the FSM is the single gatekeeper.
    """

    serializer_class = WorkOrderSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["property", "status", "priority", "category", "origin", "area"]
    search_fields = ["title", "description", "property__name", "contractor_name"]
    ordering_fields = ["created_at", "priority", "scheduled_date", "sla_due_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        user = self.request.user
        base = WorkOrder.objects.select_related(
            "property", "unit", "unit__holding", "area", "reported_by"
        ).prefetch_related("images", "comments__author")

        if hasattr(user, "landlord_profile"):
            from rentium.users.access import accessible_properties, scope_q

            # WorkOrder has no landlord FK, so it is scoped through what it
            # points at. A job in SHARED space points at a unit and has no
            # listing at all — scoping only by property/lease made those
            # invisible to the landlord who filed them (the ticket existed,
            # the dashboard said "No work orders yet").
            scope = scope_q(
                user,
                landlord_field=None,
                property_field="property",
                lease_field="lease",
            )
            # The unit's own owner, plus whole-portfolio grants — the same
            # listing-or-unit rule WorkOrder.objects.for_landlord encodes,
            # expressed through scope_q so co-landlord grants still apply.
            scope |= scope_q(user, landlord_field="unit__landlord")
            # A property-scoped co-landlord sees shared-space jobs on the unit
            # their granted listing belongs to.
            scope |= Q(unit__offerings__in=accessible_properties(user))
            return base.filter(scope).distinct()

        if hasattr(user, "tenant_profile"):
            tenant = user.tenant_profile
            active_leases = Lease.objects.filter(
                status=Lease.LeaseStatus.ACTIVE, lease_tenants__tenant=tenant
            )
            # Properties this tenant rents through an active lease:
            #  - Lease.property (reverse name: "leases") for direct
            #    property leases (complete units / single rooms), and
            #  - LeaseTenant.room (reverse name: "room_tenants") for the
            #    tenant's OWN room on a group/roommate lease. Scoped to
            #    room_tenants__tenant=tenant so a roommate's exclusive
            #    areas never leak into this tenant's visible set.
            rooms = Property.objects.filter(
                Q(leases__in=active_leases)
                | (
                    Q(room_tenants__lease__in=active_leases)
                    & Q(room_tenants__tenant=tenant)
                )
            ).distinct()
            visible_areas = PropertyArea.objects.none()
            for room in rooms:
                visible_areas = visible_areas | areas_for_tenant_room(room)

            # A fault in shared space is filed against the UNIT, so a tenant
            # whose room sits in that unit must see it even though the ticket
            # names no listing of theirs.
            unit_ids = [r.unit_id for r in rooms if r.unit_id]
            return base.filter(
                Q(reported_by=user)
                | Q(area__in=visible_areas)
                | (Q(area__isnull=True) & Q(property__in=rooms))
                | (Q(area__isnull=True) & Q(unit_id__in=unit_ids))
            ).distinct()

        return WorkOrder.objects.none()

    def perform_create(self, serializer):
        origin = serializer.validated_data.get("origin")
        if hasattr(self.request.user, "landlord_profile") and not origin:
            origin = WorkOrder.Origin.LANDLORD
        serializer.save(
            reported_by=self.request.user, origin=origin or WorkOrder.Origin.TENANT
        )

    def perform_destroy(self, instance):
        raise PermissionDenied(
            "Work orders are never deleted (they feed the property's history). Cancel it instead."
        )

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        """POST {status: 'SCHEDULED'} — the only way to change status."""
        if not hasattr(request.user, "landlord_profile"):
            raise PermissionDenied(
                "Only the landlord can change a work order's status."
            )
        work_order = self.get_object()
        new_status = request.data.get("status")
        if not new_status:
            raise ValidationError({"status": "New status is required."})
        try:
            work_order.transition_to(new_status, by=request.user)
        except DjangoValidationError as exc:
            raise ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else str(exc)
            )
        return Response(self.get_serializer(work_order).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """
        POST {cost?, post_expense?: bool, vendor?} — transition to COMPLETED
        and, if a cost is given with post_expense, book the EXPENSE ledger
        entry linked to this job in the same call.
        """
        if not hasattr(request.user, "landlord_profile"):
            raise PermissionDenied("Only the landlord can complete a work order.")
        work_order = self.get_object()

        cost = request.data.get("cost")
        if cost not in (None, ""):
            work_order.cost = cost
            work_order.save(update_fields=["cost", "updated_at"])

        try:
            work_order.transition_to(WorkOrder.Status.COMPLETED, by=request.user)
        except DjangoValidationError as exc:
            raise ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else str(exc)
            )

        if cost not in (None, "") and request.data.get("post_expense"):
            from rentium.ledger.services import post_expense

            post_expense(
                landlord=request.user.landlord_profile,
                property=work_order.property,
                amount=cost,
                category="MAINTENANCE",
                description=f"Work order: {work_order.title}",
                vendor=request.data.get("vendor", work_order.contractor_name),
                work_order=work_order,
                idempotency_key=f"woexp:{work_order.pk}",
                created_by=request.user,
            )
        return Response(self.get_serializer(work_order).data)

    @action(detail=True, methods=["post"])
    def add_comment(self, request, pk=None):
        work_order = self.get_object()
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response(
                {"body": "Comment text is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        comment = WorkOrderComment.objects.create(
            work_order=work_order, author=request.user, body=body
        )
        return Response(
            WorkOrderCommentSerializer(comment).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def add_image(self, request, pk=None):
        work_order = self.get_object()
        image = request.FILES.get("image")
        if not image:
            return Response(
                {"image": "An image file is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        obj = WorkOrderImage.objects.create(
            work_order=work_order,
            image=image,
            caption=request.data.get("caption", ""),
            uploaded_by=request.user,
        )
        return Response(
            WorkOrderImageSerializer(obj).data, status=status.HTTP_201_CREATED
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def areas_view(request):
    """
    GET /api/maintenance/areas/?property=<id>

    The areas the current user may attach a work order to on that property:
    landlords see everything on their property/group; tenants see their
    room's common/exclusive/system areas.
    """
    prop_id = request.query_params.get("property")
    if not prop_id:
        raise ValidationError({"property": "property query param is required."})
    try:
        prop = Property.objects.get(pk=prop_id)
    except (Property.DoesNotExist, DjangoValidationError, ValueError):
        raise ValidationError({"property": "Unknown property."})

    user = request.user
    if hasattr(user, "landlord_profile"):
        if prop.landlord != user.landlord_profile:
            raise PermissionDenied("Not your property.")
        if prop.group_id:
            areas = PropertyArea.objects.filter(
                Q(property=prop) | Q(group_id=prop.group_id) | Q(unit_id=prop.unit_id)
            )
        else:
            areas = PropertyArea.objects.filter(property=prop)
    elif hasattr(user, "tenant_profile"):
        areas = areas_for_tenant_room(prop)
    else:
        areas = PropertyArea.objects.none()

    return Response(AreaSerializer(areas.order_by("kind", "name"), many=True).data)
