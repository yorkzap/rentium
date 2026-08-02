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
    DepositDeduction,
    InspectionItem,
    InspectionKeyRow,
    InspectionPass,
)
from rentium.leases.models import Lease, LeaseTenant

from .inspection_serializers import (
    CustomItemSerializer,
    DepositDeductionSerializer,
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


def _clean(instance):
    """Run model validation and surface it as a 400 rather than a 500."""
    from django.core.exceptions import ValidationError as DjangoValidationError
    from rest_framework import serializers as drf_serializers

    try:
        instance.full_clean(exclude=["created_by"])
    except DjangoValidationError as exc:
        raise ValidationError(drf_serializers.as_serializer_error(exc))


class ConditionInspectionViewSet(viewsets.ModelViewSet):
    """
    list / retrieve             landlord: theirs; tenant: their own
    create {lease, lease_tenant?}                       (landlord)
    partial_update              header boxes only, open passes only (landlord)
    POST {id}/items_bulk/       [{id, move_in_condition_code, ...}]
    POST {id}/add_item/         {section, label}        (landlord)
    POST {id}/keys_bulk/        [{id?, key_type, issued_count, ...}]
    GET/POST {id}/deductions/   deposit-deduction lines  (landlord)
    PATCH/DELETE {id}/deductions/{line_pk}/              (landlord)
    POST {id}/agree_deductions/ {signed_on?}             (landlord)
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

    # ------------------------------------------------- deposit deductions
    #
    # What the landlord proposes to keep, costed line by line, attached to the
    # report row it came from. Recording lines keeps NO money: the deposit is
    # only reduced once `agree_deductions` stamps the tenant's written consent
    # (or the move-out request carries an RTB file number). See
    # MoveOutRequest.deposit_status() — there are three lawful routes out and
    # this is not a fourth.
    @action(detail=True, methods=["get", "post"], url_path="deductions")
    def deductions(self, request, pk=None):
        self._landlord()
        inspection = self.get_object()
        if request.method == "GET":
            return Response(
                DepositDeductionSerializer(
                    inspection.deposit_deductions.all(), many=True
                ).data
            )

        self._deductions_editable(inspection)
        payload = DepositDeductionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        line = DepositDeduction(
            inspection=inspection,
            created_by=request.user,
            **payload.validated_data,
        )
        _clean(line)
        line.save()
        inspection.refresh_deduction_totals()
        return Response(
            DepositDeductionSerializer(line).data, status=status.HTTP_201_CREATED
        )

    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path="deductions/(?P<line_pk>[^/.]+)",
    )
    def deduction_detail(self, request, pk=None, line_pk=None):
        self._landlord()
        inspection = self.get_object()
        self._deductions_editable(inspection)
        line = inspection.deposit_deductions.filter(pk=line_pk).first()
        if line is None:
            raise ValidationError({"detail": "No such deduction on this inspection."})

        if request.method == "DELETE":
            line.delete()
            inspection.refresh_deduction_totals()
            return Response(status=status.HTTP_204_NO_CONTENT)

        payload = DepositDeductionSerializer(line, data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        for field, value in payload.validated_data.items():
            setattr(line, field, value)
        _clean(line)
        line.save()
        inspection.refresh_deduction_totals()
        return Response(DepositDeductionSerializer(line).data)

    @action(detail=True, methods=["post"], url_path="agree_deductions")
    def agree_deductions(self, request, pk=None):
        """Record that the tenant agreed IN WRITING to these deductions.

        Body: {signed_on: YYYY-MM-DD}. This is the consent that lets deposit
        money be kept at all, so it freezes the totals as they stand: a line
        edited afterwards no longer matches what was agreed, and the mismatch
        is visible rather than silent.
        """
        from datetime import datetime

        from django.utils import timezone

        self._landlord()
        inspection = self.get_object()
        totals = inspection.deduction_totals()
        if not any(amount > 0 for amount in totals.values()):
            raise ValidationError(
                {"detail": "There are no deduction lines to agree to."}
            )

        raw = request.data.get("signed_on")
        if raw:
            try:
                signed = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
            except ValueError:
                raise ValidationError({"signed_on": "Use YYYY-MM-DD."})
            stamp = timezone.make_aware(
                datetime.combine(signed, datetime.min.time())
            )
        else:
            stamp = timezone.now()

        inspection.deduction_agreed_at = stamp
        inspection.save(update_fields=["deduction_agreed_at", "updated_at"])
        inspection.refresh_deduction_totals()
        return Response(InspectionDetailSerializer(inspection).data)

    def _deductions_editable(self, inspection):
        """Once the tenant has signed off, the figures are a signed agreement.

        Changing them then is exactly the silent edit the RTB tells you to
        replace with an addendum — so it is refused, not warned about.
        """
        if inspection.deduction_agreed_at:
            raise ValidationError(
                {
                    "detail": (
                        "The tenant has already agreed to these deductions in "
                        "writing. Changing them now would alter a signed "
                        "agreement — record a new agreement instead."
                    )
                }
            )

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
