"""
Move-out / end-of-tenancy API.

Endpoints:
  GET  /api/leases/<id>/moveout-rules/    -> tenancy_rules.rules_payload(lease)
  GET  /api/leases/moveouts/?lease=<id>   -> requests visible to me
  POST /api/leases/moveouts/              -> role-aware create
  POST /api/leases/moveouts/<id>/accept/  {rent_handling?, effective_end_date?}
  POST /api/leases/moveouts/<id>/decline/ {reason}
  POST /api/leases/moveouts/<id>/cancel/

Create semantics (business rules live in tenancy_rules.py; this view routes):

TENANT posting {lease, requested_end_date, reason?, request_mutual?}:
  - date >= earliest_tenant_end_date  -> TENANT_NOTICE, auto-ACCEPTED.
  - date too soon + request_mutual    -> MUTUAL_AGREEMENT PENDING, tenant
    pre-signed; landlord accepts & signs, or declines.
  - date too soon, no request_mutual  -> 400 carrying earliest_end_date.

LANDLORD posting {lease, kind, requested_end_date, reason?, rent_handling?}:
  - LANDLORD_NOTICE  -> validated against earliest_landlord_end_date, applied.
  - MUTUAL_AGREEMENT -> PENDING, landlord pre-signed.
"""

from __future__ import annotations

from datetime import date

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rentium.leases.models import Lease
from rentium.leases.models import MoveOutRequest
from rentium.leases.tenancy_rules import earliest_landlord_end_date
from rentium.leases.tenancy_rules import earliest_tenant_end_date
from rentium.leases.tenancy_rules import rules_for_lease
from rentium.leases.tenancy_rules import rules_payload

# A tenancy can only be ENDED if it has actually begun (or is at least fully
# papered). DRAFT is not a tenancy; nothing to give notice on.
ENDABLE_STATUSES = (
    Lease.LeaseStatus.ACTIVE,
    Lease.LeaseStatus.PENDING_SIGNATURES,  # <- the enum member. NOT `.PENDING`.
)


def _role(request):
    """-> ("LANDLORD", landlord_profile) | ("TENANT", tenant_profile)"""
    if hasattr(request.user, "landlord_profile"):
        return "LANDLORD", request.user.landlord_profile
    if hasattr(request.user, "tenant_profile"):
        return "TENANT", request.user.tenant_profile
    raise PermissionDenied("No landlord or tenant profile on this account.")


def _parse_date(value, field):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValidationError({field: "Enter a valid date (YYYY-MM-DD)."})


class MoveOutRequestSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    rent_handling_display = serializers.CharField(
        source="get_rent_handling_display", read_only=True
    )
    lease_number = serializers.CharField(source="lease.lease_number", read_only=True)
    tenant_name = serializers.SerializerMethodField()
    settlement_display = serializers.CharField(
        source="get_deposit_settlement_display", read_only=True
    )
    # The 15-day clock: when it starts, when it runs out, and what must happen
    # before then. Computed server-side so no client has to re-derive a rule
    # whose penalty is double the deposit.
    deposit_status = serializers.SerializerMethodField()

    class Meta:
        model = MoveOutRequest
        fields = [
            "id",
            "lease",
            "lease_number",
            "lease_tenant",
            "tenant_name",
            "initiated_by",
            "kind",
            "kind_display",
            "status",
            "status_display",
            "requested_end_date",
            "effective_end_date",
            "reason",
            "decline_reason",
            "form_type",
            "rent_handling",
            "rent_handling_display",
            "tenant_signed",
            "tenant_signed_at",
            "landlord_signed",
            "landlord_signed_at",
            "rules_snapshot",
            "forwarding_address",
            "forwarding_address_received_on",
            "deposit_settlement",
            "settlement_display",
            "tenant_agreement_signed_on",
            "rtb_file_number",
            "deposit_status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            f
            for f in fields
            if f
            not in (
                "lease",
                "requested_end_date",
                "reason",
                # Settled through the /settle_deposit/ action, which validates
                # that the chosen route is actually evidenced.
            )
        ]

    def get_deposit_status(self, obj):
        return obj.deposit_status()

    def get_tenant_name(self, obj):
        lt = obj.lease_tenant
        if not lt:
            return None
        return lt.display_name


class MoveOutViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = MoveOutRequestSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        role, profile = _role(self.request)
        qs = MoveOutRequest.objects.select_related(
            "lease", "lease_tenant__tenant__user"
        )
        if role == "LANDLORD":
            qs = qs.filter(lease__landlord=profile)
        else:
            qs = qs.filter(lease__lease_tenants__tenant=profile).distinct()

        lease_id = self.request.query_params.get("lease")
        if lease_id:
            qs = qs.filter(lease_id=lease_id)
        return qs

    # ------------------------------------------------------------- create
    def create(self, request, *args, **kwargs):
        role, profile = _role(request)

        lease = Lease.objects.filter(pk=request.data.get("lease")).first()
        if not lease:
            raise ValidationError({"lease": "Lease not found."})

        if lease.status not in ENDABLE_STATUSES:
            raise ValidationError(
                {
                    "lease": (
                        "Only a live tenancy can be ended this way. This lease is "
                        f"{lease.get_status_display().lower()}."
                    )
                }
            )

        requested_end = _parse_date(
            request.data.get("requested_end_date"), "requested_end_date"
        )
        if requested_end < date.today():
            raise ValidationError(
                {"requested_end_date": "The end date cannot be in the past."}
            )

        if lease.moveout_requests.filter(status=MoveOutRequest.Status.PENDING).exists():
            raise ValidationError(
                {
                    "detail": (
                        "There is already a pending move-out request on this lease. "
                        "Resolve or cancel it first."
                    )
                }
            )

        reason = (request.data.get("reason") or "").strip()
        rules = rules_for_lease(lease)
        snapshot = rules_payload(lease)

        # ------------------------------------------------------- tenant
        if role == "TENANT":
            lt = lease.lease_tenants.filter(tenant=profile).first()
            if not lt:
                raise PermissionDenied("You are not a tenant on this lease.")

            # You cannot give notice to end an agreement you never entered.
            # Previously this was unguarded, so an invited-but-unsigned tenant
            # could terminate a tenancy they had no part in.
            if not lt.has_signed:
                raise ValidationError(
                    {
                        "detail": (
                            "You haven't signed this agreement yet, so there's "
                            "nothing to give notice on. If you don't want the "
                            "place, decline the agreement instead."
                        )
                    }
                )
            if lt.declined:
                raise ValidationError(
                    {"detail": "You declined this agreement — nothing to end."}
                )

            earliest = earliest_tenant_end_date(lease)

            if requested_end >= earliest:
                # Valid notice — accepted automatically, no approval step.
                mo = MoveOutRequest.objects.create(
                    lease=lease,
                    lease_tenant=lt,
                    initiated_by=MoveOutRequest.InitiatedBy.TENANT,
                    kind=MoveOutRequest.Kind.TENANT_NOTICE,
                    requested_end_date=requested_end,
                    reason=reason,
                    rules_snapshot=snapshot,
                )
                mo.sign(as_landlord=False)
                mo.accept()  # saves + applies + publishes lease.moveout_accepted
                return Response(
                    self.get_serializer(mo).data, status=status.HTTP_201_CREATED
                )

            if not request.data.get("request_mutual"):
                raise ValidationError(
                    {
                        "requested_end_date": (
                            f"With notice given today, the earliest this tenancy can "
                            f"end is {earliest.isoformat()} "
                            f"({rules.tenant_notice_months} clear month(s)). Pick that "
                            f"date or later — or submit a "
                            f"{rules.mutual_agreement_form} mutual-agreement request "
                            f"for an earlier date, which your landlord may accept or "
                            f"decline."
                        ),
                        "earliest_end_date": earliest.isoformat(),
                    }
                )

            mo = MoveOutRequest.objects.create(
                lease=lease,
                lease_tenant=lt,
                initiated_by=MoveOutRequest.InitiatedBy.TENANT,
                kind=MoveOutRequest.Kind.MUTUAL_AGREEMENT,
                requested_end_date=requested_end,
                reason=reason,
                form_type=rules.mutual_agreement_form,
                rules_snapshot=snapshot,
            )
            mo.sign(as_landlord=False)
            mo.save()
            mo._publish("lease.moveout_requested")
            return Response(
                self.get_serializer(mo).data, status=status.HTTP_201_CREATED
            )

        # ----------------------------------------------------- landlord
        if lease.landlord != profile:
            raise PermissionDenied("Not your lease.")

        kind = request.data.get("kind") or MoveOutRequest.Kind.MUTUAL_AGREEMENT
        if kind not in (
            MoveOutRequest.Kind.LANDLORD_NOTICE,
            MoveOutRequest.Kind.MUTUAL_AGREEMENT,
        ):
            raise ValidationError(
                {"kind": "kind must be LANDLORD_NOTICE or MUTUAL_AGREEMENT."}
            )

        rent_handling = (
            request.data.get("rent_handling") or MoveOutRequest.RentHandling.NONE
        )
        if rent_handling not in MoveOutRequest.RentHandling.values:
            raise ValidationError({"rent_handling": "Invalid rent handling."})

        if kind == MoveOutRequest.Kind.LANDLORD_NOTICE:
            earliest = earliest_landlord_end_date(lease)
            if requested_end < earliest:
                msg = (
                    f"With notice served today, the earliest end date is "
                    f"{earliest.isoformat()}"
                    + (
                        f" ({rules.landlord_notice_months} clear month(s) for "
                        f"landlord use). "
                        if rules.landlord_notice_months
                        else ". "
                    )
                    + f"For an earlier date, propose a "
                    f"{rules.mutual_agreement_form} mutual agreement instead."
                )
                raise ValidationError(
                    {
                        "requested_end_date": msg,
                        "earliest_end_date": earliest.isoformat(),
                    }
                )

            mo = MoveOutRequest.objects.create(
                lease=lease,
                initiated_by=MoveOutRequest.InitiatedBy.LANDLORD,
                kind=kind,
                requested_end_date=requested_end,
                reason=reason,
                rent_handling=rent_handling,
                rules_snapshot=snapshot,
            )
            mo.sign(as_landlord=True)
            mo.accept()
            return Response(
                self.get_serializer(mo).data, status=status.HTTP_201_CREATED
            )

        mo = MoveOutRequest.objects.create(
            lease=lease,
            initiated_by=MoveOutRequest.InitiatedBy.LANDLORD,
            kind=kind,
            requested_end_date=requested_end,
            reason=reason,
            form_type=rules.mutual_agreement_form,
            rent_handling=rent_handling,
            rules_snapshot=snapshot,
        )
        mo.sign(as_landlord=True)
        mo.save()
        mo._publish("lease.moveout_requested")
        return Response(self.get_serializer(mo).data, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------ actions
    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        """
        Countersign a pending mutual agreement. Only the party that has NOT
        signed yet can accept. The landlord may also pass rent_handling and an
        (earlier) effective_end_date when accepting.
        """
        mo = self.get_object()
        role, profile = _role(request)

        if mo.status != MoveOutRequest.Status.PENDING:
            raise ValidationError({"detail": "This request is no longer pending."})

        rent_handling = None
        effective = None

        if role == "LANDLORD":
            if mo.landlord_signed:
                raise ValidationError(
                    {"detail": "You already signed; awaiting the tenant."}
                )
            rent_handling = request.data.get("rent_handling") or None
            if (
                rent_handling
                and rent_handling not in MoveOutRequest.RentHandling.values
            ):
                raise ValidationError({"rent_handling": "Invalid rent handling."})

            if request.data.get("effective_end_date"):
                effective = _parse_date(
                    request.data["effective_end_date"], "effective_end_date"
                )
                if effective > mo.requested_end_date:
                    raise ValidationError(
                        {
                            "effective_end_date": (
                                "Cannot be later than the requested date — "
                                "decline instead."
                            )
                        }
                    )
                if effective < date.today():
                    raise ValidationError(
                        {"effective_end_date": "Cannot be in the past."}
                    )
            mo.sign(as_landlord=True)
        else:
            if mo.tenant_signed:
                raise ValidationError(
                    {"detail": "You already signed; awaiting the landlord."}
                )
            if not mo.lease.lease_tenants.filter(
                tenant=profile, has_signed=True
            ).exists():
                raise PermissionDenied("You are not a signed tenant on this lease.")
            mo.sign(as_landlord=False)

        try:
            mo.accept(effective_end_date=effective, rent_handling=rent_handling)
        except DjangoValidationError as e:
            raise ValidationError(serializers.as_serializer_error(e))

        return Response(self.get_serializer(mo).data)

    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        mo = self.get_object()
        role, _profile = _role(request)

        initiated_by_landlord = mo.initiated_by == MoveOutRequest.InitiatedBy.LANDLORD
        # Only the counterparty declines; the initiator cancels instead.
        if (role == "LANDLORD") == initiated_by_landlord:
            raise ValidationError(
                {"detail": "You initiated this request — cancel it instead."}
            )

        try:
            mo.decline(reason=(request.data.get("reason") or "").strip())
        except DjangoValidationError as e:
            raise ValidationError(serializers.as_serializer_error(e))
        return Response(self.get_serializer(mo).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        mo = self.get_object()
        role, _profile = _role(request)

        initiated_by_landlord = mo.initiated_by == MoveOutRequest.InitiatedBy.LANDLORD
        if (role == "LANDLORD") != initiated_by_landlord:
            raise ValidationError(
                {"detail": "Only the party that opened the request can cancel it."}
            )

        try:
            mo.cancel()
        except DjangoValidationError as e:
            raise ValidationError(serializers.as_serializer_error(e))
        return Response(self.get_serializer(mo).data)


    @action(detail=True, methods=["post"], url_path="settle_deposit")
    def settle_deposit(self, request, pk=None):
        """Record the forwarding address, and/or how the deposit was settled.

        Deliberately NOT a plain PATCH. Each settlement route has to be
        evidenced — a written agreement has a date, an RTB application has a
        file number — because "settled" with nothing behind it is exactly the
        record that loses a dispute.
        """
        move_out = self.get_object()
        if not hasattr(request.user, "landlord_profile"):
            raise PermissionDenied("Only the landlord can settle a deposit.")

        fields = ["updated_at"]
        address = request.data.get("forwarding_address")
        if address is not None:
            move_out.forwarding_address = str(address)[:2000]
            fields.append("forwarding_address")
        received = request.data.get("forwarding_address_received_on")
        if received:
            try:
                move_out.forwarding_address_received_on = date.fromisoformat(
                    str(received)[:10]
                )
            except ValueError:
                raise ValidationError(
                    {"forwarding_address_received_on": "Use YYYY-MM-DD."}
                )
            fields.append("forwarding_address_received_on")

        settlement = request.data.get("deposit_settlement")
        if settlement:
            valid = {c for c, _ in MoveOutRequest.DepositSettlement.choices}
            if settlement not in valid:
                raise ValidationError(
                    {"deposit_settlement": f"Must be one of {sorted(valid)}."}
                )
            if settlement == MoveOutRequest.DepositSettlement.TENANT_AGREED:
                signed = request.data.get("tenant_agreement_signed_on")
                if not signed:
                    raise ValidationError(
                        {
                            "tenant_agreement_signed_on": (
                                "A written agreement needs the date the tenant "
                                "signed it."
                            )
                        }
                    )
                move_out.tenant_agreement_signed_on = date.fromisoformat(
                    str(signed)[:10]
                )
                fields.append("tenant_agreement_signed_on")
            if settlement == MoveOutRequest.DepositSettlement.RTB_APPLIED:
                file_no = str(request.data.get("rtb_file_number") or "").strip()
                if not file_no:
                    raise ValidationError(
                        {"rtb_file_number": "An RTB application has a file number."}
                    )
                move_out.rtb_file_number = file_no[:50]
                fields.append("rtb_file_number")
            move_out.deposit_settlement = settlement
            fields.append("deposit_settlement")

        move_out.save(update_fields=fields)
        return Response(self.get_serializer(move_out).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lease_moveout_rules(request, lease_id):
    """The resolved tenancy rules + earliest end dates for this lease."""
    role, profile = _role(request)

    lease = Lease.objects.filter(pk=lease_id).first()
    if not lease:
        raise ValidationError({"lease": "Lease not found."})

    if role == "LANDLORD" and lease.landlord != profile:
        raise PermissionDenied("Not your lease.")
    if role == "TENANT" and not lease.lease_tenants.filter(tenant=profile).exists():
        raise PermissionDenied("Not your lease.")

    return Response(rules_payload(lease))
