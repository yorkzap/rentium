# rentium/leases/api/inspection_views.py
"""
/api/leases/inspections/ — condition inspection endpoints.

Thin views: every business rule (prefill, locking-by-signature, write-back,
suggestions) lives in leases/inspection_services.py. Landlords manage their
leases' inspections; tenants can read their own and perform exactly two
writes: sign (with agree/disagree) per pass. All bulk saves are one round
trip — this gets filled in standing in a hallway on a phone.
"""

from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rentium.leases import inspection_services as services
from rentium.leases.inspections import (
    ConditionInspection,
    InspectionItem,
    InspectionKeyRow,
    InspectionPass,
)
from rentium.leases.models import Lease, LeaseTenant

from .inspection_serializers import (
    CustomItemSerializer,
    InspectionCreateSerializer,
    InspectionDetailSerializer,
    InspectionItemSerializer,
    InspectionKeyRowSerializer,
    InspectionListSerializer,
    ItemBulkRowSerializer,
    KeyBulkRowSerializer,
    SignSerializer,
)


def _svc(callable_, *args, **kwargs):
    """Translate business-rule violations into clean 400s."""
    try:
        return callable_(*args, **kwargs)
    except services.InspectionError as exc:
        raise ValidationError({"detail": str(exc)})


class ConditionInspectionViewSet(viewsets.ModelViewSet):
    """
    list / retrieve             landlord: theirs; tenant: their own
    create {lease, lease_tenant?}                       (landlord)
    partial_update              header boxes only, open passes only (landlord)
    POST {id}/items_bulk/       [{id, move_in_condition_code, ...}]
    POST {id}/add_item/         {section, label}        (landlord)
    POST {id}/keys_bulk/        [{id?, key_type, issued_count, ...}]
    POST {id}/landlord_sign/    {inspection_pass, name}
    POST {id}/tenant_sign/      {inspection_pass, name, agrees, reason?}
    POST {id}/start_move_out/   {move_out_date?}
    POST {id}/mark_delivered/   {inspection_pass}       (compliance clock)
    GET  suggestions/?status=PENDING                    (landlord)
    POST {id}/items/{item_pk}/approve_suggestion/
    POST {id}/items/{item_pk}/dismiss_suggestion/
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self):
        if self.action == "list":
            return InspectionListSerializer
        return InspectionDetailSerializer

    def get_queryset(self):
        user = self.request.user
        base = ConditionInspection.objects.select_related(
            "lease", "lease_tenant__tenant__user", "lease_tenant__room", "template"
        ).prefetch_related("items__area", "key_rows")
        if hasattr(user, "landlord_profile"):
            from rentium.users.access import scope_q

            qs = base.filter(
                scope_q(user, landlord_field=None, lease_field="lease")
            ).distinct()
        elif hasattr(user, "tenant_profile"):
            tenant = user.tenant_profile
            # Their own room inspections, plus complete-unit inspections
            # (lease_tenant is NULL) on leases they're linked to. A roommate
            # never sees another roommate's document.
            qs = base.filter(
                Q(lease_tenant__tenant=tenant)
                | (
                    Q(lease_tenant__isnull=True)
                    & Q(lease__lease_tenants__tenant=tenant)
                )
            )
        else:
            return ConditionInspection.objects.none()
        lease_id = self.request.query_params.get("lease")
        if lease_id:
            qs = qs.filter(lease_id=lease_id)
        wanted = self.request.query_params.get("status")
        if wanted:
            qs = qs.filter(status=wanted)
        return qs.distinct()

    # -------------------------------------------------------------- helpers
    def _landlord(self):
        if not hasattr(self.request.user, "landlord_profile"):
            raise PermissionDenied("Landlords only.")
        return self.request.user.landlord_profile

    def _tenant_may_sign(self, inspection) -> bool:
        user = self.request.user
        if not hasattr(user, "tenant_profile"):
            return False
        tenant = user.tenant_profile
        if inspection.lease_tenant_id:
            return inspection.lease_tenant.tenant_id == tenant.pk
        # Complete-unit doc: any linked tenant on the lease may sign for the
        # household (the typed name records exactly who did).
        return inspection.lease.lease_tenants.filter(tenant=tenant).exists()

    # --------------------------------------------------------------- create
    def create(self, request, *args, **kwargs):
        landlord = self._landlord()
        payload = InspectionCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        lease = Lease.objects.filter(
            pk=payload.validated_data["lease"], landlord=landlord
        ).first()
        if not lease:
            raise ValidationError({"lease": "Not your lease."})
        lease_tenant = None
        lt_id = payload.validated_data.get("lease_tenant")
        if lt_id:
            lease_tenant = LeaseTenant.objects.filter(pk=lt_id, lease=lease).first()
            if not lease_tenant:
                raise ValidationError({"lease_tenant": "Not on this lease."})
        inspection = _svc(
            services.build_inspection,
            lease=lease,
            lease_tenant=lease_tenant,
            created_by=request.user,
        )
        return Response(
            InspectionDetailSerializer(inspection, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    # ------------------------------------------------ header-box PATCH gate
    def partial_update(self, request, *args, **kwargs):
        self._landlord()
        inspection = self.get_object()
        # Header fields tied to a signed pass are frozen with it.
        move_in_fields = {
            "possession_date", "move_in_inspection_date",
            "tenant_agent_move_in", "repairs_required_at_start",
        }
        move_out_fields = {
            "move_out_date", "move_out_inspection_date", "tenant_agent_move_out",
            "tenant_responsible_damage", "tenant_forwarding_address",
        }
        touched = set(request.data.keys())
        if inspection.pass_is_locked(InspectionPass.MOVE_IN) and touched & move_in_fields:
            raise ValidationError(
                {"detail": "The move-in pass is fully signed and locked. Corrections need an addendum, not edits."}
            )
        if inspection.pass_is_locked(InspectionPass.MOVE_OUT) and touched & move_out_fields:
            raise ValidationError(
                {"detail": "The move-out pass is fully signed and locked."}
            )
        return super().partial_update(request, *args, **kwargs)

    # ----------------------------------------------------------- bulk items
    @action(detail=True, methods=["post"])
    def items_bulk(self, request, pk=None):
        """One round trip for a whole section's worth of edits. Landlord
        only; each pass's columns reject writes once that pass is signed."""
        self._landlord()
        inspection = self.get_object()
        rows = ItemBulkRowSerializer(data=request.data, many=True)
        rows.is_valid(raise_exception=True)

        move_in_locked = inspection.pass_is_locked(InspectionPass.MOVE_IN)
        move_out_locked = inspection.pass_is_locked(InspectionPass.MOVE_OUT)
        items_by_id = {str(i.pk): i for i in inspection.items.all()}
        move_in_cols = {
            "move_in_condition_code", "move_in_cleanliness_code", "move_in_comment",
        }
        move_out_cols = {
            "move_out_condition_code", "move_out_cleanliness_code", "move_out_comment",
        }

        updated = []
        for row in rows.validated_data:
            item = items_by_id.get(str(row["id"]))
            if item is None:
                raise ValidationError({"id": f"Item {row['id']} is not on this inspection."})
            touched = set(row.keys()) - {"id"}
            if move_in_locked and touched & move_in_cols:
                raise ValidationError(
                    {"detail": "Move-in columns are locked (pass fully signed)."}
                )
            if move_out_locked and touched & move_out_cols:
                raise ValidationError(
                    {"detail": "Move-out columns are locked (pass fully signed)."}
                )
            for field in touched:
                setattr(item, field, row[field])
            updated.append(item)

        InspectionItem.objects.bulk_update(
            updated,
            [
                "move_in_condition_code", "move_in_cleanliness_code", "move_in_comment",
                "move_out_condition_code", "move_out_cleanliness_code", "move_out_comment",
                "needs_attention",
            ],
        )
        inspection.refresh_from_db()
        return Response(
            InspectionItemSerializer(inspection.items.all(), many=True).data
        )

    @action(detail=True, methods=["post"])
    def add_item(self, request, pk=None):
        self._landlord()
        inspection = self.get_object()
        if inspection.status == ConditionInspection.Status.COMPLETED:
            raise ValidationError({"detail": "This inspection is completed and locked."})
        payload = CustomItemSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        last = inspection.items.order_by("-sort_order").first()
        item = InspectionItem.objects.create(
            inspection=inspection,
            section=payload.validated_data["section"],
            label=payload.validated_data["label"],
            sort_order=(last.sort_order + 10) if last else 10,
            is_custom=True,
        )
        return Response(
            InspectionItemSerializer(item).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def keys_bulk(self, request, pk=None):
        self._landlord()
        inspection = self.get_object()
        rows = KeyBulkRowSerializer(data=request.data, many=True)
        rows.is_valid(raise_exception=True)
        existing = {str(k.pk): k for k in inspection.key_rows.all()}
        out = []
        next_sort = (max((k.sort_order for k in existing.values()), default=0)) + 10
        for row in rows.validated_data:
            row_id = str(row["id"]) if row.get("id") else None
            if row_id and row_id in existing:
                key = existing[row_id]
                key.key_type = row["key_type"]
                key.issued_count = row["issued_count"]
                key.returned_count = row.get("returned_count")
                key.save(update_fields=["key_type", "issued_count", "returned_count"])
            else:
                key = InspectionKeyRow.objects.create(
                    inspection=inspection,
                    key_type=row["key_type"],
                    issued_count=row["issued_count"],
                    returned_count=row.get("returned_count"),
                    sort_order=next_sort,
                )
                next_sort += 10
            out.append(key)
        return Response(InspectionKeyRowSerializer(out, many=True).data)

    # ------------------------------------------------------------ signatures
    @action(detail=True, methods=["post"])
    def landlord_sign(self, request, pk=None):
        self._landlord()
        inspection = self.get_object()
        payload = SignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        _svc(
            services.record_signature,
            inspection,
            pass_name=payload.validated_data["inspection_pass"],
            role="LANDLORD",
            signature_name=payload.validated_data["name"],
        )
        inspection.refresh_from_db()
        return Response(
            InspectionDetailSerializer(inspection, context={"request": request}).data
        )

    @action(detail=True, methods=["post"])
    def tenant_sign(self, request, pk=None):
        inspection = self.get_object()
        if not self._tenant_may_sign(inspection):
            raise PermissionDenied("Only a tenant on this inspection can sign it.")
        payload = SignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        _svc(
            services.record_signature,
            inspection,
            pass_name=payload.validated_data["inspection_pass"],
            role="TENANT",
            signature_name=payload.validated_data["name"],
            agrees=payload.validated_data.get("agrees"),
            disagreement_reason=payload.validated_data.get("reason", ""),
        )
        inspection.refresh_from_db()
        return Response(
            InspectionDetailSerializer(inspection, context={"request": request}).data
        )

    # ------------------------------------------------------------- move-out
    @action(detail=True, methods=["post"])
    def start_move_out(self, request, pk=None):
        self._landlord()
        inspection = self.get_object()
        from datetime import datetime

        move_out_date = None
        if request.data.get("move_out_date"):
            try:
                move_out_date = datetime.strptime(
                    str(request.data["move_out_date"]), "%Y-%m-%d"
                ).date()
            except ValueError:
                raise ValidationError({"move_out_date": "Use YYYY-MM-DD."})
        _svc(services.start_move_out, inspection, move_out_date=move_out_date)
        inspection.refresh_from_db()
        return Response(
            InspectionDetailSerializer(inspection, context={"request": request}).data
        )

    @action(detail=True, methods=["post"])
    def mark_delivered(self, request, pk=None):
        """Stamp the 7-/15-day compliance clock: 'I gave the tenant their copy'."""
        from django.utils import timezone

        self._landlord()
        inspection = self.get_object()
        pass_name = request.data.get("inspection_pass")
        if pass_name == InspectionPass.MOVE_IN:
            inspection.move_in_report_delivered_at = timezone.now()
            inspection.save(update_fields=["move_in_report_delivered_at", "updated_at"])
        elif pass_name == InspectionPass.MOVE_OUT:
            inspection.move_out_report_delivered_at = timezone.now()
            inspection.save(update_fields=["move_out_report_delivered_at", "updated_at"])
        else:
            raise ValidationError({"inspection_pass": "MOVE_IN or MOVE_OUT."})
        return Response(
            InspectionDetailSerializer(inspection, context={"request": request}).data
        )

    # ----------------------------------------------------------- suggestions
    @action(detail=False, methods=["get"])
    def suggestions(self, request):
        """Landlord's pending maintenance suggestions across the portfolio."""
        self._landlord()
        wanted = request.query_params.get("status", InspectionItem.SuggestionStatus.PENDING)
        items = (
            InspectionItem.objects.filter(
                inspection__lease__landlord=request.user.landlord_profile,
                suggestion_status=wanted,
            )
            .select_related("inspection__lease", "area")
            .order_by("-updated_at")
        )
        return Response(InspectionItemSerializer(items, many=True).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="items/(?P<item_pk>[^/.]+)/approve_suggestion",
    )
    def approve_suggestion(self, request, pk=None, item_pk=None):
        self._landlord()
        inspection = self.get_object()
        item = inspection.items.filter(pk=item_pk).first()
        if not item:
            raise ValidationError({"detail": "Item not found on this inspection."})
        work_order = _svc(services.approve_suggestion, item, user=request.user)
        return Response(
            {
                "item": InspectionItemSerializer(item).data,
                "work_order_id": str(work_order.pk),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="items/(?P<item_pk>[^/.]+)/dismiss_suggestion",
    )
    def dismiss_suggestion(self, request, pk=None, item_pk=None):
        self._landlord()
        inspection = self.get_object()
        item = inspection.items.filter(pk=item_pk).first()
        if not item:
            raise ValidationError({"detail": "Item not found on this inspection."})
        _svc(services.dismiss_suggestion, item)
        return Response(InspectionItemSerializer(item).data)
