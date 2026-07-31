import calendar
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import ProtectedError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import status
from rest_framework import viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rentium.core.phone import to_e164
from rentium.leases.models import Lease
from rentium.leases.models import LeaseDocument
from rentium.leases.models import LeaseTenant
from rentium.leases.models import Payment
from rentium.leases.models import PaymentReminder
from rentium.leases.models import RentAdjustment
from rentium.leases.services import compute_rent_split
from rentium.properties.models import Property
from rentium.users.models import TenantProfile

from .permissions import IsLandlordOrTenantMember
from .permissions import IsLandlordOwner
from .permissions import LeaseNotLocked
from .serializers import LeaseDocumentSerializer
from .serializers import LeaseListSerializer
from .serializers import LeaseSerializer
from .serializers import LeaseTenantSerializer
from .serializers import PaymentReminderSerializer
from .serializers import PaymentSerializer
from .serializers import RentAdjustmentSerializer
from .serializers import TenantBasicSerializer

# NOTE ON THE PDF:
#
# This module used to carry a ~200-line build_lease_pdf() and a `pdf` @action that
# served it. Both are gone.
#
# That function was a SECOND, independent implementation of "what does this lease
# say" — the frontend had a third, in leaseFormats.ts. Three renderings of one legal
# document, already drifting, which meant a tenant could sign one thing on screen
# and download a different thing afterwards. It even carried a disclaimer admitting
# it was only "a summary of the lease terms on file".
#
# There is now exactly one renderer (leases/documents.py). The screen renders it,
# leases/pdf.py renders the same object, and api/urls.py already routes
# /leases/<id>/pdf/ to document_views.lease_pdf. Nothing imported the old function;
# it was dead code that could only ever rot.


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lease_types_view(request):
    """
    Returns the lease types offered for NEW leases, with the property category and
    province each applies to.

    Room agreements are intentionally province-agnostic: every room lease (private
    or shared) uses the single GENERIC_ROOMMATE "Standard Roommate Agreement",
    regardless of which province the property is in.

    BC_ROOMMATE_AGREEMENT and SK_ROOMMATE_AGREEMENT still exist on Lease.LeaseType
    for leases created before this change, but aren't offered here for new ones —
    see the RETIRED_ROOM_TYPES exclusion below.

    Complete-unit agreements stay province-specific.
    """
    RETIRED_ROOM_TYPES = {"BC_ROOMMATE", "SK_ROOMMATE"}

    PROVINCE_MAPPING = {
        "BC": ["BC_RESIDENTIAL"],
        "SK": ["SK_RESIDENTIAL"],
        "GENERIC": ["GENERIC_RESIDENTIAL"],
    }
    province_lookup = {}
    for province_code, lease_keys in PROVINCE_MAPPING.items():
        for lease_type in lease_keys:
            province_lookup[lease_type] = province_code

    FULL_PROVINCE_NAMES = {
        "BC": "British Columbia",
        "SK": "Saskatchewan",
        "GENERIC": "Other / Generic",
    }

    lease_types = []
    for value, label in Lease.LeaseType.choices:
        if value in RETIRED_ROOM_TYPES:
            continue

        if value == "GENERIC_ROOMMATE":
            lease_types.append(
                {
                    "value": value,
                    "label": label,
                    "property_category": Property.PropertyCategory.ROOM,
                    "province": {"code": "GENERIC", "name": "All Provinces"},
                }
            )
            continue

        province_code = province_lookup.get(value, "GENERIC")
        lease_types.append(
            {
                "value": value,
                "label": label,
                "property_category": Property.PropertyCategory.COMPLETE_UNIT,
                "province": {
                    "code": province_code,
                    "name": FULL_PROVINCE_NAMES.get(province_code, "Unknown"),
                },
            }
        )

    return Response(lease_types)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def check_overlap_view(request):
    """
    Pre-creation warning check — NOT used to block or auto-resolve anything, just to
    surface a heads-up while the landlord is still picking property/dates in the
    create-lease form.

    Query params: property (id) OR group (id), start_date (YYYY-MM-DD),
    end_date (YYYY-MM-DD, omit for month-to-month).
    """
    property_id = request.query_params.get("property")
    group_id = request.query_params.get("group")
    start_date_str = request.query_params.get("start_date")
    end_date_str = request.query_params.get("end_date")

    if not (property_id or group_id) or not start_date_str:
        return Response(
            {"error": "property (or group) and start_date are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        start_date = timezone.datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = (
            timezone.datetime.strptime(end_date_str, "%Y-%m-%d").date()
            if end_date_str
            else None
        )
    except ValueError:
        return Response(
            {"error": "Invalid date format. Use YYYY-MM-DD."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # An unsaved, throwaway Lease, purely to reuse its own overlap logic
    # (Lease.get_overlapping_leases() / _overlaps()) rather than duplicating it.
    probe = Lease(
        property_id=property_id or None,
        group_id=group_id or None,
        start_date=start_date,
        end_date=end_date,
    )
    overlaps = probe.get_overlapping_leases()

    return Response(
        {
            "has_overlap": len(overlaps) > 0,
            "overlapping_leases": LeaseListSerializer(overlaps, many=True).data,
        }
    )


class LeaseViewSet(viewsets.ModelViewSet):
    """ViewSet for managing leases. Supports different views for landlords and tenants."""

    permission_classes = [IsLandlordOrTenantMember, LeaseNotLocked]
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = [
        "status",
        "lease_type",
        "is_month_to_month",
        "property",
        "group",
    ]
    search_fields = [
        "lease_number",
        "property__name",
        "group__name",
        "property__address",
    ]
    ordering_fields = ["start_date", "end_date", "created_at", "lease_number"]
    ordering = ["-start_date"]

    def get_serializer_class(self):
        if self.action == "list":
            return LeaseListSerializer
        return LeaseSerializer

    def get_queryset(self):
        from rentium.users.access import accessible_leases

        user = self.request.user
        base_queryset = Lease.objects.select_related(
            "property", "group", "landlord__user"
        ).prefetch_related(
            "lease_tenants__tenant__user",
            "lease_tenants__room",
            "lease_tenants__rent_adjustments",
            "landlord_signatories__member",
            "additional_documents",
            "payments__tenant__user",
        )

        # A landlord sees their own leases plus any granted to them as a
        # co-landlord (scoped to a property/group). Falls closed for everyone
        # else via accessible_leases returning an empty queryset.
        accessible = accessible_leases(user)
        if accessible.exists() or hasattr(user, "landlord_profile"):
            allowed_ids = list(accessible.values_list("pk", flat=True))
            return base_queryset.filter(pk__in=allowed_ids)

        if hasattr(user, "tenant_profile"):
            return base_queryset.filter(
                Q(lease_tenants__tenant=user.tenant_profile)
                | Q(
                    lease_tenants__tenant__isnull=True,
                    lease_tenants__invited_email__iexact=user.email,
                )
            ).distinct()

        return Lease.objects.none()

    def perform_create(self, serializer):
        if not hasattr(self.request.user, "landlord_profile"):
            raise PermissionDenied("Only landlords can create leases.")
        # Inherit the property's default bills/utilities when the new lease didn't
        # specify any, so a landlord who set them on the property doesn't have to
        # re-enter them for every future lease.
        extra = {}
        if not serializer.validated_data.get("bills_included"):
            prop = serializer.validated_data.get("property")
            if prop is not None and getattr(prop, "default_bills_included", None):
                extra["bills_included"] = prop.default_bills_included
        serializer.save(landlord=self.request.user.landlord_profile, **extra)

    def destroy(self, request, *args, **kwargs):
        """
        Deletion is intentionally narrow: only a DRAFT lease can be deleted outright.
        Anything past DRAFT has real tenant invites/signatures attached and should be
        resolved via `terminate` instead, which preserves the record rather than
        erasing it — leases are the kind of thing you want an audit trail for even
        when they didn't work out.
        """
        lease = self.get_object()
        if lease.status != Lease.LeaseStatus.DRAFT:
            raise PermissionDenied(
                'Only draft leases can be deleted. Use "Terminate" for a lease that '
                "already has tenants invited or is active."
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            raise ValidationError(
                "This draft can't be deleted because it already has payment records "
                "attached to it."
            )

    @action(detail=False, methods=["get"])
    def bill_providers(self, request):
        """
        A structured list of common utility bill types, categories, and providers by
        region, for the lease creation form.
        """
        bill_providers_by_region = {
            "BC": {
                "electricity": {
                    "display_name": "Electricity",
                    "providers": [
                        {"id": "bc_hydro", "name": "BC Hydro"},
                        {"id": "fortis", "name": "FortisBC (Electricity)"},
                        {"id": "other_electricity", "name": "Other Provider"},
                    ],
                },
                "gas": {
                    "display_name": "Natural Gas",
                    "providers": [
                        {"id": "fortis_gas", "name": "FortisBC Gas"},
                        {"id": "pacific_northern", "name": "Pacific Northern Gas"},
                        {"id": "other_gas", "name": "Other Provider"},
                    ],
                },
                "water": {
                    "display_name": "Water",
                    "providers": [
                        {"id": "city_utilities", "name": "City Utilities"},
                        {"id": "saanich", "name": "Saanich Utilities"},
                        {"id": "crd", "name": "CRD Water"},
                        {"id": "other_water", "name": "Other Provider"},
                    ],
                },
                "heat": {
                    "display_name": "Heating",
                    "providers": [
                        {"id": "bc_hydro_heat", "name": "BC Hydro (Electric Heat)"},
                        {"id": "fortis_heat", "name": "FortisBC (Gas Heat)"},
                        {"id": "building_heat", "name": "Building Managed"},
                        {"id": "other_heat", "name": "Other Provider"},
                    ],
                },
                "internet": {
                    "display_name": "Internet",
                    "providers": [
                        {"id": "telus", "name": "Telus"},
                        {"id": "shaw", "name": "Shaw"},
                        {"id": "rogers", "name": "Rogers"},
                        {"id": "bell", "name": "Bell"},
                        {"id": "other_internet", "name": "Other Provider"},
                    ],
                },
                "waste": {
                    "display_name": "Waste Collection",
                    "providers": [
                        {"id": "city_waste", "name": "City Waste Collection"},
                        {"id": "building_waste", "name": "Building Managed"},
                        {"id": "other_waste", "name": "Other Provider"},
                    ],
                },
            },
            "SK": {
                "electricity": {
                    "display_name": "Electricity",
                    "providers": [
                        {"id": "saskpower", "name": "SaskPower"},
                        {"id": "other_electricity", "name": "Other Provider"},
                    ],
                },
                "gas": {
                    "display_name": "Natural Gas",
                    "providers": [
                        {"id": "saskenergy", "name": "SaskEnergy"},
                        {"id": "other_gas", "name": "Other Provider"},
                    ],
                },
                "water": {
                    "display_name": "Water",
                    "providers": [
                        {"id": "city_utilities", "name": "City Utilities"},
                        {"id": "saskatoon_water", "name": "Saskatoon Water"},
                        {"id": "other_water", "name": "Other Provider"},
                    ],
                },
                "heat": {
                    "display_name": "Heating",
                    "providers": [
                        {"id": "saskpower_heat", "name": "SaskPower (Electric Heat)"},
                        {"id": "saskenergy_heat", "name": "SaskEnergy (Gas Heat)"},
                        {"id": "building_heat", "name": "Building Managed"},
                        {"id": "other_heat", "name": "Other Provider"},
                    ],
                },
                "internet": {
                    "display_name": "Internet",
                    "providers": [
                        {"id": "sasktel", "name": "SaskTel"},
                        {"id": "shaw", "name": "Shaw"},
                        {"id": "access", "name": "Access Communications"},
                        {"id": "other_internet", "name": "Other Provider"},
                    ],
                },
                "waste": {
                    "display_name": "Waste Collection",
                    "providers": [
                        {"id": "city_waste", "name": "City Waste Collection"},
                        {"id": "building_waste", "name": "Building Managed"},
                        {"id": "other_waste", "name": "Other Provider"},
                    ],
                },
            },
            "GENERIC": {
                "electricity": {
                    "display_name": "Electricity",
                    "providers": [
                        {"id": "local_electric", "name": "Local Electric Utility"},
                        {"id": "other_electricity", "name": "Other Provider"},
                    ],
                },
                "gas": {
                    "display_name": "Natural Gas",
                    "providers": [
                        {"id": "local_gas", "name": "Local Gas Utility"},
                        {"id": "other_gas", "name": "Other Provider"},
                    ],
                },
                "water": {
                    "display_name": "Water",
                    "providers": [
                        {"id": "city_water", "name": "City/Municipal Water"},
                        {"id": "other_water", "name": "Other Provider"},
                    ],
                },
                "heat": {
                    "display_name": "Heating",
                    "providers": [
                        {"id": "electric_heat", "name": "Electric Heat"},
                        {"id": "gas_heat", "name": "Gas Heat"},
                        {"id": "oil_heat", "name": "Oil Heat"},
                        {"id": "building_heat", "name": "Building Managed"},
                        {"id": "other_heat", "name": "Other Provider"},
                    ],
                },
                "internet": {
                    "display_name": "Internet",
                    "providers": [
                        {"id": "local_isp", "name": "Local ISP"},
                        {"id": "national_isp", "name": "National Provider"},
                        {"id": "other_internet", "name": "Other Provider"},
                    ],
                },
                "waste": {
                    "display_name": "Waste Collection",
                    "providers": [
                        {"id": "city_waste", "name": "City/Municipal Waste Collection"},
                        {"id": "building_waste", "name": "Building Managed"},
                        {"id": "other_waste", "name": "Other Provider"},
                    ],
                },
            },
        }

        responsibility_types = [
            {"id": "none", "name": "None - Included in Rent"},
            {"id": "full", "name": "Full - Tenant Pays 100%"},
            {"id": "percentage", "name": "Percentage - Tenant Pays a Portion"},
            {"id": "fixed", "name": "Fixed Amount - Tenant Pays Set Fee"},
        ]

        distribution_methods = [
            {"id": "none", "name": "None (Not Applicable)"},
            {"id": "equal", "name": "Equal Split Among All Tenants"},
            {"id": "weighted", "name": "Weighted by Rent Amount"},
            {"id": "custom", "name": "Custom Percentages per Tenant"},
        ]

        return Response(
            {
                "bill_providers": bill_providers_by_region,
                "responsibility_types": responsibility_types,
                "distribution_methods": distribution_methods,
            }
        )

    @action(detail=True, methods=["get"])
    def calculate_bill_share(self, request, pk=None):
        """
        Calculate a tenant's share of a specific utility bill.

        Query parameters: tenant_id, bill_type, amount.
        """
        lease = self.get_object()
        tenant_id = request.query_params.get("tenant_id")
        bill_type = request.query_params.get("bill_type")
        amount_str = request.query_params.get("amount")

        if not tenant_id or not bill_type or not amount_str:
            return Response(
                {"error": "Missing required parameters: tenant_id, bill_type, amount."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            amount = Decimal(amount_str)
            if amount < 0:
                return Response(
                    {"error": "Bill amount cannot be negative."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid amount format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant_lease = get_object_or_404(lease.lease_tenants, tenant__id=tenant_id)

        if bill_type not in lease.bills_included:
            return Response(
                {"error": f"Bill type '{bill_type}' not found in this lease."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant_share = lease.calculate_tenant_bill_share(tenant_id, bill_type, amount)
        bill_details = lease.bills_included.get(bill_type, {})
        calculation_details = self._generate_calculation_explanation(
            lease, tenant_id, bill_type, amount, tenant_share
        )

        return Response(
            {
                "tenant_id": tenant_id,
                "tenant_name": tenant_lease.tenant.user.name
                if tenant_lease.tenant
                else None,
                "lease_number": lease.lease_number,
                "bill_type": bill_type,
                "bill_provider": bill_details.get("provider", ""),
                "total_amount": float(amount),
                "tenant_share": float(tenant_share),
                "calculation_details": calculation_details,
            }
        )

    def _generate_calculation_explanation(
        self, lease, tenant_id, bill_type, amount, tenant_share
    ):
        """A human-readable explanation of how the bill share was calculated."""
        bill_details = lease.bills_included.get(bill_type, {})
        if not bill_details:
            return {"explanation": "Bill details not found"}

        tenant_count = lease.lease_tenants.count()

        if bill_details.get("included", False):
            return {
                "responsibility_type": "none",
                "explanation": "Bill is included in rent. No additional payment required.",
            }

        resp = bill_details.get("tenant_responsibility", {})
        resp_type = resp.get("type")
        distribution = resp.get("distribution")

        if resp_type == "none":
            return {
                "responsibility_type": "none",
                "explanation": "No tenant responsibility for this bill.",
            }

        explanation = ""
        if resp_type == "full":
            value = 100
            explanation = "Tenant responsible for full bill"
        elif resp_type == "percentage":
            value = resp.get("value", 0)
            explanation = f"Tenant responsible for {value}% of bill"
        elif resp_type == "fixed":
            value = resp.get("value", 0)
            explanation = f"Tenant pays fixed amount of ${value}"
            if distribution == "equal":
                explanation += f" divided equally among {tenant_count} tenants"
            return {
                "responsibility_type": resp_type,
                "responsibility_value": value,
                "distribution_method": distribution,
                "tenant_count": tenant_count,
                "explanation": explanation,
                "calculation": f"Fixed amount: ${value}"
                + (
                    f" ÷ {tenant_count} tenants = ${float(tenant_share)}"
                    if distribution == "equal" and tenant_count > 1
                    else ""
                ),
            }
        else:
            value = 0

        if distribution == "equal" and tenant_count > 1:
            explanation += f", divided equally among {tenant_count} tenants"
            calculation = f"${float(amount)} × {value}% ÷ {tenant_count} tenants = ${float(tenant_share)}"
        elif distribution == "custom":
            custom_splits = resp.get("custom_splits", {})
            tenant_percentage = custom_splits.get(str(tenant_id), 0)
            explanation += (
                f", with custom split of {tenant_percentage}% for this tenant"
            )
            calculation = f"${float(amount)} × {value}% × {tenant_percentage}% = ${float(tenant_share)}"
        elif distribution == "weighted":
            explanation += ", weighted by each tenant's rent amount"
            try:
                tenant_lease = lease.lease_tenants.get(tenant__id=tenant_id)
                total_rent = lease.get_total_monthly_rent()
                tenant_weight = (
                    (tenant_lease.rent_amount / total_rent) * 100
                    if total_rent > 0
                    else 0
                )
                calculation = (
                    f"${float(amount)} × {value}% × {tenant_weight:.1f}% rent weight "
                    f"= ${float(tenant_share)}"
                )
            except Exception:
                calculation = (
                    f"${float(amount)} × {value}% with rent-weighted distribution "
                    f"= ${float(tenant_share)}"
                )
        else:
            calculation = f"${float(amount)} × {value}% = ${float(tenant_share)}"

        return {
            "responsibility_type": resp_type,
            "responsibility_value": value,
            "distribution_method": distribution,
            "tenant_count": tenant_count,
            "explanation": explanation,
            "calculation": calculation,
        }

    @action(detail=True, methods=["get"])
    def all_bill_shares(self, request, pk=None):
        """
        All tenants' shares for each bill type with a given amount.

        Query param: bill_amounts, a JSON object e.g. {"electricity": 150, "gas": 80}
        """
        lease = self.get_object()

        try:
            bill_amounts = json.loads(request.query_params.get("bill_amounts", "{}"))
        except json.JSONDecodeError:
            return Response(
                {"error": "Invalid bill_amounts format. Provide a valid JSON object."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not bill_amounts:
            return Response(
                {"error": "No bill amounts provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        results = {}
        for bill_type, amount in bill_amounts.items():
            if bill_type not in lease.bills_included:
                continue

            bill_results = {}
            for lease_tenant in lease.lease_tenants.all():
                if not lease_tenant.tenant_id:
                    continue
                tenant_id = str(lease_tenant.tenant.id)
                tenant_share = lease.calculate_tenant_bill_share(
                    tenant_id, bill_type, Decimal(amount)
                )
                bill_results[tenant_id] = {
                    "tenant_name": lease_tenant.tenant.user.name,
                    "share_amount": float(tenant_share),
                }

            results[bill_type] = {
                "total_amount": float(amount),
                "provider": lease.bills_included[bill_type].get("provider", ""),
                "tenant_shares": bill_results,
            }

        return Response(results)

    @action(detail=True, methods=["post"], permission_classes=[IsLandlordOwner])
    def create_utility_payment(self, request, pk=None):
        """
        Create utility bill payments for tenants based on their calculated shares.

        NOTE: this writes to the legacy Payment model. New code should use the
        ledger's /api/ledger/utility-bills/ endpoint, which handles joint household
        billing and occupancy-weighted splits. Kept for backward compatibility.
        """
        lease = self.get_object()

        required_fields = [
            "bill_type",
            "total_amount",
            "utility_provider",
            "due_date",
            "period_start",
            "period_end",
        ]
        missing = [f for f in required_fields if f not in request.data]
        if missing:
            return Response(
                {"error": f"Missing required fields: {', '.join(missing)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        bill_type = request.data["bill_type"]
        utility_provider = request.data["utility_provider"]

        try:
            total_amount = Decimal(request.data["total_amount"])
            due_date = timezone.datetime.strptime(
                request.data["due_date"], "%Y-%m-%d"
            ).date()
            period_start = timezone.datetime.strptime(
                request.data["period_start"], "%Y-%m-%d"
            ).date()
            period_end = timezone.datetime.strptime(
                request.data["period_end"], "%Y-%m-%d"
            ).date()
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid date or amount format. Use YYYY-MM-DD for dates."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if bill_type not in lease.bills_included:
            return Response(
                {"error": f"Bill type '{bill_type}' not found in this lease."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant_ids = request.data.get("tenant_ids", [])
        if tenant_ids:
            tenant_leases = lease.lease_tenants.filter(tenant__id__in=tenant_ids)
        else:
            tenant_leases = lease.lease_tenants.exclude(tenant__isnull=True)

        if not tenant_leases.exists():
            return Response(
                {"error": "No valid tenants found for creating payments."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created_payments = []
        for tenant_lease in tenant_leases:
            tenant_id = str(tenant_lease.tenant.id)
            tenant_share = lease.calculate_tenant_bill_share(
                tenant_id, bill_type, total_amount
            )
            if tenant_share <= 0:
                continue

            payment = Payment.objects.create(
                lease=lease,
                tenant=tenant_lease.tenant,
                payment_type=Payment.PaymentType.UTILITY,
                amount_due=tenant_share,
                due_date=due_date,
                status=Payment.PaymentStatus.PENDING,
                utility_type=bill_type,
                utility_provider=utility_provider,
                utility_period_start=period_start,
                utility_period_end=period_end,
                notes=(
                    f"Utility payment for {utility_provider} ({bill_type}) — "
                    f"{period_start} to {period_end}"
                ),
            )
            created_payments.append(PaymentSerializer(payment).data)

        return Response(
            {
                "message": f"Created {len(created_payments)} utility payments.",
                "payments": created_payments,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def terminate(self, request, pk=None):
        """
        Terminate an active or pending lease.

        Ending a lease must also end its OPEN RECEIVABLES — otherwise the financial
        summary keeps counting a dead lease's rent as "expected" and its charges sit
        Overdue forever, which is how a landlord's numbers quietly stop meaning
        anything. Charges with money already on them are left alone: received money
        is historical fact.
        """
        lease = self.get_object()

        if lease.status in [
            Lease.LeaseStatus.TERMINATED,
            Lease.LeaseStatus.EXPIRED,
            Lease.LeaseStatus.RENEWED,
        ]:
            raise ValidationError(
                f"Lease is already in a final state ({lease.status})."
            )

        termination_date_str = request.data.get("termination_date")
        move_out_date_str = request.data.get("move_out_date", termination_date_str)

        try:
            termination_date = (
                timezone.datetime.strptime(termination_date_str, "%Y-%m-%d").date()
                if termination_date_str
                else timezone.now().date()
            )
            move_out_date = (
                timezone.datetime.strptime(move_out_date_str, "%Y-%m-%d").date()
                if move_out_date_str
                else termination_date
            )
        except (ValueError, TypeError):
            raise ValidationError("Invalid date format. Use YYYY-MM-DD.")

        lease.status = Lease.LeaseStatus.TERMINATED
        lease.move_out_date = move_out_date
        if not lease.end_date or lease.end_date > termination_date:
            lease.end_date = termination_date
        lease.save()

        from rentium.leases.occupancy import close_lease_occupancies
        from rentium.ledger.billing import void_open_charges_for_lease

        void_open_charges_for_lease(
            lease,
            reason=f"Lease {lease.lease_number} terminated {termination_date}",
            created_by=request.user,
        )
        close_lease_occupancies(lease, move_out=move_out_date)

        serializer = self.get_serializer(lease)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], permission_classes=[IsLandlordOwner])
    def renew(self, request, pk=None):
        """Renew a lease by creating a new lease linked to the old one."""
        old_lease = self.get_object()

        if old_lease.status in [
            Lease.LeaseStatus.DRAFT,
            Lease.LeaseStatus.PENDING_SIGNATURES,
        ]:
            raise ValidationError(
                "Cannot renew a lease that is not yet active or finalized."
            )

        serializer = LeaseSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)

        old_lease.status = Lease.LeaseStatus.RENEWED
        old_lease.save()

        new_lease = serializer.save(
            landlord=request.user.landlord_profile,
            previous_lease=old_lease,
        )

        if request.data.get("copy_tenants", True):
            for old_lt in old_lease.lease_tenants.all():
                already_added = (
                    new_lease.lease_tenants.filter(tenant=old_lt.tenant).exists()
                    if old_lt.tenant_id
                    else new_lease.lease_tenants.filter(
                        invited_email__iexact=old_lt.invited_email
                    ).exists()
                )
                if not already_added:
                    LeaseTenant.objects.create(
                        lease=new_lease,
                        tenant=old_lt.tenant,
                        invited_email=old_lt.invited_email
                        if not old_lt.tenant_id
                        else "",
                        invited_name=old_lt.invited_name,
                        invited_phone=old_lt.invited_phone,
                        rent_amount=old_lt.rent_amount,
                        room=old_lt.room,
                        is_primary_tenant=old_lt.is_primary_tenant,
                        cleaning_fee=old_lt.cleaning_fee,
                        cleaning_fee_paid=False,
                        has_signed=False,
                    )

        return Response(
            self.get_serializer(new_lease).data, status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=["get"], permission_classes=[IsLandlordOwner])
    def available_tenants(self, request):
        """All tenants that can be added to leases."""
        tenants = TenantProfile.objects.all().select_related("user")
        return Response(TenantBasicSerializer(tenants, many=True).data)

    @action(detail=True, methods=["post"], permission_classes=[IsLandlordOwner])
    def landlord_sign(self, request, pk=None):
        """
        Landlord signs the lease. Combined with at least one tenant signature, this
        flips the lease to ACTIVE via Lease.check_and_activate() — which is what
        generates the deposit, fee and rent charges on the ledger.
        """
        lease = self.get_object()

        if lease.is_locked():
            raise PermissionDenied("This lease is already fully executed.")
        if lease.landlord_signed:
            raise ValidationError("You have already signed this lease.")

        if not lease.rent_is_fully_allocated():
            raise ValidationError(
                f"This lease's rent isn't fully assigned yet — "
                f"${lease.get_unallocated_rent()} of ${lease.total_rent} is still "
                f"unassigned across tenants. Adjust the tenant rent amounts (or add "
                f"another tenant) so they add up to the total before signing."
            )

        lease.landlord_signed = True
        lease.landlord_signed_date = timezone.now()
        lease.save(
            update_fields=["landlord_signed", "landlord_signed_date", "updated_at"]
        )

        lease.check_and_activate()

        return Response(self.get_serializer(lease).data)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def co_landlord_sign(self, request, pk=None):
        """A co-landlord (additional signing party) signs the lease. The lease
        only activates once the owner AND every co-landlord AND a tenant have
        signed (Lease.check_and_activate)."""
        lease = self.get_object()  # get_queryset already scopes to co-landlords

        if lease.is_locked():
            raise PermissionDenied("This lease is already fully executed.")

        sig = lease.landlord_signatories.filter(member=request.user).first()
        if sig is None:
            raise PermissionDenied("You are not a co-landlord on this lease.")
        if sig.has_signed:
            raise ValidationError("You have already signed this lease.")
        if not lease.rent_is_fully_allocated():
            raise ValidationError(
                f"This lease's rent isn't fully assigned yet — "
                f"${lease.get_unallocated_rent()} of ${lease.total_rent} is still "
                f"unassigned across tenants."
            )

        sig.sign()  # marks signed + attempts activation
        return Response(self.get_serializer(lease).data)

    @action(detail=True, methods=["post"], permission_classes=[IsLandlordOwner])
    def invite_co_landlord(self, request, pk=None):
        """Invite a co-landlord to co-sign THIS lease (and grant its property, so
        future leases there name them too). Mirrors the RAMA add_co_landlord flow."""
        from rentium.leases.services import grant_co_landlord

        lease = self.get_object()
        name = (request.data.get("name") or "").strip()
        email = (request.data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            raise ValidationError("A valid email is required.")
        own = getattr(request.user, "email", "").lower()
        if email == own:
            raise ValidationError("That's your own account.")
        _member, _created, emailed = grant_co_landlord(
            lease.landlord, name=name, email=email, lease=lease
        )
        lease.refresh_from_db()
        data = self.get_serializer(lease).data
        data["_emailed"] = emailed
        return Response(data)

    @action(detail=True, methods=["post"], url_path="preview-split")
    def preview_split(self, request, pk=None):
        """
        Computes what each row's rent_amount should be, given the lease's total_rent
        and the current set of tenant rows (existing + not-yet-created), without
        saving anything.

        Both the create-lease flow and the tenant-roster editor call this on every
        edit instead of each maintaining their own copy of the split algorithm
        client-side — see leases/services.py:compute_rent_split for why that
        mattered (two independent JS implementations, already drifting, and no way
        for an API caller to compute a valid split at all).

        Body: {"rows": [{"id": "<uuid>" | null, "rent_amount": "600.00" | null,
                         "touched": true | false}, ...]}

        `has_signed` is deliberately NOT accepted from the client — it's looked up
        server-side from the real LeaseTenant records for any row with an `id`, so a
        client can't (accidentally or otherwise) claim a signed tenant is editable.
        """
        lease = self.get_object()
        existing_by_id = {str(lt.id): lt for lt in lease.lease_tenants.all()}

        resolved_rows = []
        for row in request.data.get("rows", []):
            row_id = row.get("id")
            existing = existing_by_id.get(str(row_id)) if row_id else None
            raw_amount = row.get("rent_amount")
            resolved_rows.append(
                {
                    "id": row_id,
                    "rent_amount": Decimal(str(raw_amount))
                    if raw_amount not in (None, "")
                    else None,
                    "touched": bool(row.get("touched")),
                    "has_signed": bool(existing and existing.has_signed),
                }
            )

        result_rows = compute_rent_split(resolved_rows, lease.total_rent)
        allocated = sum((r["rent_amount"] for r in result_rows), Decimal("0.00"))
        unallocated = Decimal(lease.total_rent or "0.00") - allocated

        return Response(
            {
                "total_rent": str(lease.total_rent),
                "unallocated": str(unallocated),
                "rows": [
                    {
                        "id": r["id"],
                        "rent_amount": str(r["rent_amount"]),
                        "touched": r["touched"],
                        "has_signed": r["has_signed"],
                    }
                    for r in result_rows
                ],
            }
        )


class LeaseTenantViewSet(viewsets.ModelViewSet):
    """
    Tenant associations with leases, including the invite-by-email flow for tenants
    who don't have accounts yet.
    """

    serializer_class = LeaseTenantSerializer
    permission_classes = [IsLandlordOrTenantMember, LeaseNotLocked]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["lease", "tenant", "room", "has_signed", "cleaning_fee_paid"]
    ordering_fields = ["rent_amount", "signed_date", "created_at"]
    ordering = ["lease", "invited_email"]

    def get_queryset(self):
        user = self.request.user
        base = LeaseTenant.objects.select_related(
            "tenant__user", "lease", "room"
        ).prefetch_related("rent_adjustments")

        if hasattr(user, "landlord_profile"):
            from rentium.users.access import scope_q

            return base.filter(
                scope_q(user, landlord_field=None, lease_field="lease")
            ).distinct()

        if hasattr(user, "tenant_profile"):
            return base.filter(
                Q(tenant=user.tenant_profile)
                | Q(tenant__isnull=True, invited_email__iexact=user.email)
            )

        return LeaseTenant.objects.none()

    def get_serializer_context(self):
        """Add lease to context if available (nested route) or from request data."""
        context = super().get_serializer_context()
        lease = None

        if "lease_pk" in self.kwargs:
            lease = get_object_or_404(Lease, pk=self.kwargs["lease_pk"])
        elif self.request.method == "POST" and self.request.data.get("lease"):
            lease = get_object_or_404(Lease, pk=self.request.data.get("lease"))

        if lease:
            self.check_object_permissions(self.request, lease)
            context["lease"] = lease
        return context

    def perform_create(self, serializer):
        user = self.request.user
        if not hasattr(user, "landlord_profile"):
            raise PermissionDenied("Only landlords can add tenants to a lease.")

        lease = serializer.validated_data.get(
            "lease"
        ) or self.get_serializer_context().get("lease")
        if not lease:
            raise ValidationError("Lease must be specified.")
        if lease.landlord != user.landlord_profile:
            raise PermissionDenied("You can only add tenants to your own leases.")
        if lease.is_locked():
            raise PermissionDenied(
                "This lease is already fully executed and cannot be edited."
            )

        instance = serializer.save(lease=lease)
        self._auto_prorate_first_month(instance, lease, user.landlord_profile)

        # Send the invite. This used to be a `# TODO: fire invite email`, which meant
        # the invite link only worked if the landlord copy-pasted it by hand.
        email_sent = False
        if instance.invited_email and not instance.tenant_id:
            try:
                from rentium.showcase.emails import send_tenant_invite

                email_sent = send_tenant_invite(instance)
            except Exception:
                # send() never raises — it logs and returns False — but an import-time
                # failure shouldn't cost the landlord the LeaseTenant row they just
                # created. They can resend from the lease page.
                pass
        from rentium.leases.models import LeaseInviteEvent
        from rentium.leases.services import record_invite_event

        record_invite_event(
            instance,
            LeaseInviteEvent.Kind.SENT,
            actor=user,
            metadata={"email_sent": email_sent},
        )
        if instance.tenant_id:
            record_invite_event(
                instance,
                LeaseInviteEvent.Kind.ACCOUNT_LINKED,
                actor=user,
                metadata={"linked_existing_account": True},
            )

        if lease.status == Lease.LeaseStatus.DRAFT and lease.lease_tenants.count() > 0:
            lease.status = Lease.LeaseStatus.PENDING_SIGNATURES
            lease.save(update_fields=["status"])

    def _auto_prorate_first_month(self, lease_tenant, lease, landlord_profile):
        """
        Auto-generates the first-month RentAdjustment when this tenant's effective
        move-in date isn't the 1st — the classic "moved in April 25th" case.

        Without this, RentAdjustment.create_proration() exists but nothing ever calls
        it, and tenants get charged a full month's rent for a partial first month.
        """
        move_in_date = (
            lease_tenant.individual_start_date or lease.move_in_date or lease.start_date
        )
        if not move_in_date or move_in_date.day == 1:
            return

        # Idempotency guard: don't stack a second proration if this somehow runs twice
        # (a future "reprocess" action, or a manual re-save via admin).
        if lease_tenant.rent_adjustments.filter(
            adjustment_type=RentAdjustment.AdjustmentType.PRORATION
        ).exists():
            return

        last_day = calendar.monthrange(move_in_date.year, move_in_date.month)[1]
        period_end_date = move_in_date.replace(day=last_day)

        RentAdjustment.create_proration(
            lease_tenant=lease_tenant,
            move_in_date=move_in_date,
            period_end_date=period_end_date,
            created_by=landlord_profile,
        )

    @action(
        detail=True,
        methods=["get"],
        permission_classes=[AllowAny],
        url_path="invite-preview",
    )
    def invite_preview(self, request, pk=None):
        """
        Token-gated read-only preview for someone who followed their invite link but
        isn't logged in yet. Requires ?token=<invite_token>.
        """
        lease_tenant = get_object_or_404(LeaseTenant, pk=pk)
        token = request.query_params.get("token")

        if not token or str(lease_tenant.invite_token) != token:
            raise PermissionDenied("Invalid or missing invite token.")
        from rentium.leases.models import LeaseInviteEvent
        from rentium.leases.services import record_invite_event

        record_invite_event(
            lease_tenant,
            LeaseInviteEvent.Kind.LINK_OPENED,
            metadata={
                "user_agent": str(request.headers.get("User-Agent") or "")[:300],
            },
            # Invite pages re-fetch on load; don't flood the audit trail.
            debounce_seconds=120,
        )

        return Response(
            {
                "lease_tenant": LeaseTenantSerializer(lease_tenant).data,
                "lease_number": lease_tenant.lease.lease_number,
                "lease_type": lease_tenant.lease.lease_type,
                "property_address": getattr(
                    lease_tenant.lease.property, "address", None
                ),
                "group_name": getattr(lease_tenant.lease.group, "name", None),
                "rent_amount": lease_tenant.rent_amount,
                "already_signed": lease_tenant.has_signed,
            }
        )

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def claim(self, request, pk=None):
        """
        A logged-in tenant claims a pending invite slot matching their email. Call it
        right after signup, or from an "I was invited" flow.
        """
        lease_tenant = get_object_or_404(LeaseTenant, pk=pk)

        if lease_tenant.tenant_id is not None:
            return Response(
                {"detail": "This slot is already linked to an account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not lease_tenant.invited_email:
            raise PermissionDenied("This lease tenant slot has no pending invite.")
        if lease_tenant.invited_email.lower() != request.user.email.lower():
            raise PermissionDenied("This invite was sent to a different email address.")

        tenant_profile, _ = TenantProfile.objects.get_or_create(user=request.user)
        lease_tenant.tenant = tenant_profile
        lease_tenant.invite_accepted_at = timezone.now()
        lease_tenant.save(update_fields=["tenant", "invite_accepted_at", "updated_at"])
        from rentium.leases.models import LeaseInviteEvent
        from rentium.leases.services import record_invite_event

        record_invite_event(
            lease_tenant,
            LeaseInviteEvent.Kind.ACCOUNT_LINKED,
            actor=request.user,
            metadata={"source": "claim"},
        )

        return Response(self.get_serializer(lease_tenant).data)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[AllowAny],
        url_path="activate-account",
    )
    def activate_account(self, request, pk=None):
        """
        Turns a pending email invite into a real, logged-in account — this is what the
        "set your password" page reached via the invite link calls.

        Anonymous by design: the whole point is to let someone create an account for
        the first time. The invite_token is the credential proving they followed a
        legitimate link, not a login. Once `tenant` is set, this always 400s and
        points at a normal login — there's nothing left to activate, and re-running it
        would try to create a second account for the same email.
        """
        lease_tenant = get_object_or_404(LeaseTenant, pk=pk)
        token = request.data.get("token")

        if not token or str(lease_tenant.invite_token) != token:
            raise PermissionDenied("Invalid or missing invite token.")

        if lease_tenant.tenant_id is not None:
            return Response(
                {
                    "detail": "This invite has already been used to create an account. Please log in instead."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not lease_tenant.invited_email:
            raise ValidationError(
                "This lease tenant slot has no invited email to activate."
            )

        password = request.data.get("password")
        if not password:
            raise ValidationError({"password": "Password is required."})
        try:
            validate_password(password)
        except DjangoValidationError as e:
            raise ValidationError({"password": list(e.messages)})

        name = (request.data.get("name") or "").strip()

        User = get_user_model()
        existing = User.objects.filter(email__iexact=lease_tenant.invited_email).first()
        if existing is not None:
            # An extremely unlikely race (the email got registered elsewhere between
            # invite creation and now). Don't silently take over someone else's account.
            return Response(
                {
                    "detail": "An account with this email already exists. Please log in instead."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.create_user(
            email=lease_tenant.invited_email,
            password=password,
            name=name
            or lease_tenant.invited_name
            or lease_tenant.invited_email.split("@")[0],
            user_type=User.UserType.TENANT,
        )

        # Carry the phone the landlord entered on the invite, if any — it saves the
        # tenant retyping it at the sign gate, and it's the same number either way.
        if lease_tenant.invited_phone:
            user.phone = lease_tenant.invited_phone
            user.save(update_fields=["phone"])

        # Arriving via a unique, only-ever-emailed-to-them invite token is treated as
        # proof of email ownership, so we skip separate verification for this path.
        try:
            from allauth.account.models import EmailAddress

            EmailAddress.objects.update_or_create(
                user=user,
                email=user.email,
                defaults={"verified": True, "primary": True},
            )
        except ImportError:
            pass

        if hasattr(user, "is_verified"):
            user.is_verified = True
            user.save(update_fields=["is_verified"])

        tenant_profile = TenantProfile.objects.create(user=user)
        lease_tenant.tenant = tenant_profile
        lease_tenant.invite_accepted_at = timezone.now()
        lease_tenant.save(update_fields=["tenant", "invite_accepted_at", "updated_at"])
        from rentium.leases.models import LeaseInviteEvent
        from rentium.leases.services import record_invite_event

        record_invite_event(
            lease_tenant,
            LeaseInviteEvent.Kind.ACCOUNT_LINKED,
            actor=user,
            metadata={"source": "activate_account"},
        )

        auth_token, _ = Token.objects.get_or_create(user=user)

        return Response(
            {
                "token": auth_token.key,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "name": user.name,
                    "user_type": user.user_type,
                },
                "lease_tenant": self.get_serializer(lease_tenant).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def sign(self, request, pk=None):
        """
        Mark a lease as signed by the logged-in tenant (auto-claiming the slot first
        if it was only invited by email).

        Body: {phone?} — required unless the account already has a number on file.

        Uses Lease.accepts_signatures() rather than "not is_locked()": this lease
        keeps accepting signatures from not-yet-signed tenants even after it's already
        ACTIVE, per the joint-and-several activation policy. Requiring every roommate
        to sign before anyone can move in would let a single holdout block the whole
        household indefinitely.
        """
        lease_tenant = self.get_object()

        if lease_tenant.has_signed:
            raise ValidationError("This agreement is already signed.")
        if lease_tenant.declined:
            raise ValidationError(
                "This agreement was declined and can't be signed. Contact the landlord."
            )
        if not lease_tenant.lease.accepts_signatures():
            raise PermissionDenied(
                "This lease is no longer accepting signatures (expired, terminated, or renewed)."
            )
        if not lease_tenant.lease.rent_is_fully_allocated():
            raise ValidationError(
                f"This lease's rent isn't fully assigned across tenants yet — "
                f"${lease_tenant.lease.get_unallocated_rent()} of "
                f"${lease_tenant.lease.total_rent} is still unassigned. Ask your "
                f"landlord to fix the split before signing."
            )

        user = request.user

        # --- Identity check -------------------------------------------------
        linked_during_sign = lease_tenant.tenant_id is None
        if lease_tenant.tenant_id is not None:
            if (
                not hasattr(user, "tenant_profile")
                or lease_tenant.tenant != user.tenant_profile
            ):
                raise PermissionDenied("You can only sign your own lease agreement.")
        else:
            if lease_tenant.invited_email.lower() != user.email.lower():
                raise PermissionDenied("You can only sign your own lease agreement.")
            tenant_profile, _ = TenantProfile.objects.get_or_create(user=user)
            lease_tenant.tenant = tenant_profile
            lease_tenant.invite_accepted_at = timezone.now()

        # --- Phone number, captured at the moment of signing -----------------
        #
        # LeaseTenant.invited_phone and User.phone both exist, and BOTH are rendered
        # onto the agreement (see documents.py:tenant_rows). Nothing in this app has
        # ever asked the tenant for either, so both have printed blank on every lease
        # it has ever produced.
        #
        # Required here, and ONLY here. Someone filling in a settings page has no
        # reason to care about their phone number and will skip it. Someone signing a
        # tenancy agreement is providing their contact details FOR a legal document —
        # asking at that moment is asking at the only moment where the ask justifies
        # itself, and where refusing to proceed without it is defensible. If the
        # account already has a number, we don't ask again.
        raw_phone = str(request.data.get("phone") or "").strip()
        existing_phone = (getattr(user, "phone", "") or "").strip()

        if not raw_phone and not existing_phone:
            raise ValidationError(
                {
                    "phone": (
                        "A phone number is required to sign — it goes on the agreement "
                        "as your contact details."
                    )
                }
            )

        canonical = existing_phone
        if raw_phone:
            try:
                canonical = to_e164(raw_phone)
            except Exception:
                raise ValidationError(
                    {"phone": "That doesn't look like a valid phone number."}
                )
            if canonical != existing_phone:
                user.phone = canonical
                user.save(update_fields=["phone"])

        # Keep the slot's own copy in step, so the agreement still renders a phone
        # number even if the account is later unlinked.
        if canonical and lease_tenant.invited_phone != canonical:
            lease_tenant.invited_phone = canonical

        # --- Sign -----------------------------------------------------------
        lease_tenant.has_signed = True
        lease_tenant.signed_date = timezone.now()
        lease_tenant.save()
        from rentium.leases.models import LeaseInviteEvent
        from rentium.leases.services import record_invite_event

        if linked_during_sign:
            record_invite_event(
                lease_tenant,
                LeaseInviteEvent.Kind.ACCOUNT_LINKED,
                actor=user,
                metadata={"source": "sign"},
            )
        record_invite_event(
            lease_tenant,
            LeaseInviteEvent.Kind.SIGNED,
            actor=user,
        )

        # This is what activates the lease and generates the deposit/fee/rent charges.
        lease_tenant.lease.check_and_activate()

        return Response(self.get_serializer(lease_tenant).data)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def decline(self, request, pk=None):
        """
        Tenant declines to sign. Does not change the parent Lease's status — the
        landlord sees the decline on the lease detail view and decides next steps
        (remove/replace the tenant slot, or terminate).

        Blocked once the lease is ACTIVE (unlike sign(), which deliberately keeps
        working post-activation): declining doesn't make sense once the joint
        agreement is already in effect for the household. At that point it's a
        landlord decision, not a unilateral one.
        """
        lease_tenant = self.get_object()

        if lease_tenant.has_signed:
            raise ValidationError(
                "This agreement is already signed and can't be declined."
            )
        if lease_tenant.declined:
            raise ValidationError("This agreement has already been declined.")
        if lease_tenant.lease.is_locked():
            raise PermissionDenied("This lease is already fully executed.")

        user = request.user
        linked_during_decline = lease_tenant.tenant_id is None
        if lease_tenant.tenant_id is not None:
            if (
                not hasattr(user, "tenant_profile")
                or lease_tenant.tenant != user.tenant_profile
            ):
                raise PermissionDenied(
                    "You can only respond to your own lease agreement."
                )
        else:
            if lease_tenant.invited_email.lower() != user.email.lower():
                raise PermissionDenied(
                    "You can only respond to your own lease agreement."
                )
            tenant_profile, _ = TenantProfile.objects.get_or_create(user=user)
            lease_tenant.tenant = tenant_profile
            lease_tenant.invite_accepted_at = timezone.now()
            lease_tenant.save(
                update_fields=["tenant", "invite_accepted_at", "updated_at"]
            )

        lease_tenant.declined = True
        lease_tenant.declined_at = timezone.now()
        lease_tenant.decline_reason = request.data.get("reason", "")
        lease_tenant.save(
            update_fields=["declined", "declined_at", "decline_reason", "updated_at"]
        )
        from rentium.leases.models import LeaseInviteEvent
        from rentium.leases.services import record_invite_event

        if linked_during_decline:
            record_invite_event(
                lease_tenant,
                LeaseInviteEvent.Kind.ACCOUNT_LINKED,
                actor=request.user,
                metadata={"source": "decline"},
            )
        record_invite_event(
            lease_tenant,
            LeaseInviteEvent.Kind.DECLINED,
            actor=request.user,
        )

        return Response(self.get_serializer(lease_tenant).data)

    @action(detail=True, methods=["post"], permission_classes=[IsLandlordOwner])
    def resend_invite(self, request, pk=None):
        lease_tenant = self.get_object()

        if lease_tenant.tenant_id is not None:
            return Response(
                {"detail": "This slot is already linked; nothing to resend."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lease_tenant.invite_sent_at = timezone.now()
        lease_tenant.save(update_fields=["invite_sent_at", "updated_at"])

        from rentium.showcase.emails import send_tenant_invite

        sent = send_tenant_invite(lease_tenant)
        from rentium.leases.models import LeaseInviteEvent
        from rentium.leases.services import record_invite_event

        record_invite_event(
            lease_tenant,
            LeaseInviteEvent.Kind.RESENT,
            actor=request.user,
            metadata={"email_sent": sent},
        )
        return Response(
            {
                "detail": "Invite resent."
                if sent
                else (
                    "The invite was marked as resent, but the email didn't send. "
                    "Copy the invite link and send it yourself."
                ),
                "email_sent": sent,
            }
        )

    @action(detail=True, methods=["post"], permission_classes=[IsLandlordOwner])
    def mark_cleaning_fee_paid(self, request, pk=None):
        """Mark the cleaning fee as paid for a tenant."""
        lease_tenant = self.get_object()

        if lease_tenant.cleaning_fee_paid:
            raise ValidationError("Cleaning fee is already marked as paid.")
        if lease_tenant.cleaning_fee <= 0:
            raise ValidationError("No cleaning fee was set for this tenant.")

        lease_tenant.cleaning_fee_paid = True
        lease_tenant.save()

        return Response(self.get_serializer(lease_tenant).data)


class RentAdjustmentViewSet(viewsets.ModelViewSet):
    """
    Rent adjustments (proration / discounts / increases). Deliberately NOT gated by
    LeaseNotLocked — discounts and increases are meant to happen throughout an active
    tenancy, not just before signing.
    """

    serializer_class = RentAdjustmentSerializer
    permission_classes = [IsLandlordOrTenantMember]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["lease_tenant", "adjustment_type", "is_recurring"]
    ordering_fields = ["effective_date", "created_at"]
    ordering = ["-effective_date"]

    def get_queryset(self):
        user = self.request.user
        base = RentAdjustment.objects.select_related(
            "lease_tenant__lease", "created_by"
        )

        if hasattr(user, "landlord_profile"):
            from rentium.users.access import scope_q

            return base.filter(
                scope_q(user, landlord_field=None, lease_field="lease_tenant__lease")
            ).distinct()
        if hasattr(user, "tenant_profile"):
            return base.filter(lease_tenant__tenant=user.tenant_profile)
        return RentAdjustment.objects.none()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "lease_tenant_pk" in self.kwargs:
            lease_tenant = get_object_or_404(
                LeaseTenant, pk=self.kwargs["lease_tenant_pk"]
            )
            self.check_object_permissions(self.request, lease_tenant)
            context["lease_tenant"] = lease_tenant
        return context

    def perform_create(self, serializer):
        user = self.request.user
        if not hasattr(user, "landlord_profile"):
            raise PermissionDenied("Only landlords can create rent adjustments.")

        lease_tenant = serializer.validated_data.get(
            "lease_tenant"
        ) or self.get_serializer_context().get("lease_tenant")
        if not lease_tenant:
            raise ValidationError("lease_tenant must be specified.")
        if lease_tenant.lease.landlord != user.landlord_profile:
            raise PermissionDenied("You can only adjust rent on your own leases.")

        adjustment = serializer.save(
            lease_tenant=lease_tenant, created_by=user.landlord_profile
        )

        # Reconcile the existing ledger charges with the new adjustment: unpaid future
        # charges are voided and reposted at the new amount; charges with money on them
        # get a CREDIT for the difference. Without this, a discount would show on the
        # RentAdjustment record and nowhere the tenant can actually see it.
        from rentium.ledger.billing import apply_adjustment_to_ledger

        apply_adjustment_to_ledger(lease_tenant, adjustment)


class LeaseDocumentViewSet(viewsets.ModelViewSet):
    """Documents attached to a lease."""

    serializer_class = LeaseDocumentSerializer
    permission_classes = [IsLandlordOrTenantMember]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["lease", "is_signed"]
    search_fields = ["title", "description"]

    def get_queryset(self):
        user = self.request.user
        base = LeaseDocument.objects.select_related("lease")

        if hasattr(user, "landlord_profile"):
            from rentium.users.access import scope_q

            return base.filter(
                scope_q(user, landlord_field=None, lease_field="lease")
            ).distinct()
        if hasattr(user, "tenant_profile"):
            return base.filter(
                lease__lease_tenants__tenant=user.tenant_profile
            ).distinct()
        return LeaseDocument.objects.none()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "lease_pk" in self.kwargs:
            lease = get_object_or_404(Lease, pk=self.kwargs["lease_pk"])
            self.check_object_permissions(self.request, lease)
            context["lease"] = lease
        return context

    def perform_create(self, serializer):
        user = self.request.user
        if not hasattr(user, "landlord_profile"):
            raise PermissionDenied("Only landlords can upload lease documents.")

        lease = serializer.validated_data.get(
            "lease"
        ) or self.get_serializer_context().get("lease")
        if not lease:
            raise ValidationError("Lease must be specified.")
        if lease.landlord != user.landlord_profile:
            raise PermissionDenied("You can only upload documents to your own leases.")

        serializer.save(lease=lease)


class PaymentViewSet(viewsets.ModelViewSet):
    """
    The LEGACY Payment model.

    The ledger (/api/ledger/) is the single source of financial truth — computed
    status, append-only, joint household charges, occupancy-weighted splits. This
    viewset survives for historical rows and any code still pointing at it. New work
    goes to the ledger.
    """

    serializer_class = PaymentSerializer
    permission_classes = [IsLandlordOrTenantMember]
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = {
        "lease": ["exact"],
        "tenant": ["exact"],
        "payment_type": ["exact", "in"],
        "status": ["exact", "in"],
        "due_date": ["exact", "gte", "lte"],
        "payment_date": ["exact", "gte", "lte", "isnull"],
        "utility_type": ["exact", "isnull"],
    }
    search_fields = ["reference_number", "notes", "utility_provider"]
    ordering_fields = ["due_date", "payment_date", "amount_due", "created_at"]
    ordering = ["due_date"]

    def get_queryset(self):
        user = self.request.user
        base = Payment.objects.select_related("lease", "tenant__user").prefetch_related(
            "reminders"
        )

        if hasattr(user, "landlord_profile"):
            from rentium.users.access import scope_q

            return base.filter(
                scope_q(user, landlord_field=None, lease_field="lease")
            ).distinct()
        if hasattr(user, "tenant_profile"):
            return base.filter(tenant=user.tenant_profile)
        return Payment.objects.none()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "lease_pk" in self.kwargs:
            lease = get_object_or_404(Lease, pk=self.kwargs["lease_pk"])
            self.check_object_permissions(self.request, lease)
            context["lease"] = lease
        return context

    def perform_create(self, serializer):
        user = self.request.user
        if not hasattr(user, "landlord_profile"):
            raise PermissionDenied("Only landlords can record payments.")

        lease = serializer.validated_data.get(
            "lease"
        ) or self.get_serializer_context().get("lease")
        if not lease:
            raise ValidationError("Lease must be specified.")
        if lease.landlord != user.landlord_profile:
            raise PermissionDenied("You can only record payments for your own leases.")

        serializer.save(lease=lease)

    @action(detail=True, methods=["post"], permission_classes=[IsLandlordOwner])
    def mark_as_paid(self, request, pk=None):
        payment = self.get_object()

        if payment.status == Payment.PaymentStatus.COMPLETED:
            raise ValidationError("Payment is already marked as completed.")

        payment.amount_paid = payment.amount_due
        payment.payment_date = request.data.get("payment_date") or timezone.now().date()
        payment.payment_method = (
            request.data.get("payment_method")
            or payment.payment_method
            or Payment.PaymentMethod.OTHER
        )
        payment.reference_number = request.data.get(
            "reference_number", payment.reference_number
        )
        payment.notes = request.data.get("notes", payment.notes)
        payment.save()

        return Response(self.get_serializer(payment).data)

    @action(detail=True, methods=["post"], permission_classes=[IsLandlordOwner])
    def refund(self, request, pk=None):
        payment = self.get_object()

        if payment.status not in [
            Payment.PaymentStatus.COMPLETED,
            Payment.PaymentStatus.PARTIALLY_PAID,
        ]:
            raise ValidationError(
                "Can only refund payments that were completed or partially paid."
            )

        payment.status = Payment.PaymentStatus.REFUNDED
        refund_notes = request.data.get("notes", "Payment Refunded")
        payment.notes += (
            f"\n---\nRefunded on {timezone.now().date()}. Reason: {refund_notes}"
        )
        payment.save()

        return Response(self.get_serializer(payment).data)


class PaymentReminderViewSet(viewsets.ModelViewSet):
    """Scheduled reminders for upcoming or overdue payments."""

    serializer_class = PaymentReminderSerializer
    permission_classes = [IsLandlordOwner]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["payment", "is_sent", "reminder_date"]
    ordering_fields = ["reminder_date", "sent_date"]
    ordering = ["reminder_date"]

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, "landlord_profile"):
            return PaymentReminder.objects.filter(
                payment__lease__landlord=user.landlord_profile
            ).select_related("payment")
        return PaymentReminder.objects.none()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "payment_pk" in self.kwargs:
            payment = get_object_or_404(Payment, pk=self.kwargs["payment_pk"])
            self.check_object_permissions(self.request, payment)
            context["payment"] = payment
        return context

    def perform_create(self, serializer):
        user = self.request.user
        payment = serializer.validated_data.get(
            "payment"
        ) or self.get_serializer_context().get("payment")
        if not payment:
            raise ValidationError("Payment must be specified.")
        if payment.lease.landlord != user.landlord_profile:
            raise PermissionDenied(
                "Cannot create reminders for payments not belonging to your leases."
            )
        serializer.save(payment=payment)

    @action(detail=True, methods=["post"])
    def mark_as_sent(self, request, pk=None):
        reminder = self.get_object()
        if reminder.is_sent:
            raise ValidationError("Reminder is already marked as sent.")

        reminder.is_sent = True
        reminder.sent_date = timezone.now()
        reminder.save()

        return Response(self.get_serializer(reminder).data)
