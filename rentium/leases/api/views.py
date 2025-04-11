from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from decimal import Decimal
import json

from rentium.leases.models import (
    Lease, LeaseTenant, LeaseDocument, Payment, PaymentReminder
)
from rentium.users.models import TenantProfile
from .permissions import IsLandlordOwner, IsLandlordOrTenantMember
from .serializers import (
    LeaseSerializer, LeaseListSerializer, LeaseTenantSerializer,
    LeaseDocumentSerializer, PaymentSerializer, PaymentReminderSerializer,
    TenantBasicSerializer, UtilityBillSerializer
)
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rentium.properties.models import Property


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def lease_types_view(request):
    """
    Returns all available lease types along with the required property category
    and matching province (e.g., BC or SK).
    """
    ROOM_TYPES = {"BC_ROOMMATE", "SK_ROOMMATE", "GENERIC_ROOMMATE"}
    PROVINCE_MAPPING = {
        "BC": ["BC_ROOMMATE", "BC_RESIDENTIAL"],
        "SK": ["SK_ROOMMATE", "SK_RESIDENTIAL"],
        "GENERIC": ["GENERIC_ROOMMATE", "GENERIC_RESIDENTIAL"]
    }
    province_lookup = {}
    for province_code, lease_keys in PROVINCE_MAPPING.items():
        for lease_type in lease_keys:
            province_lookup[lease_type] = province_code
    FULL_PROVINCE_NAMES = {
        "BC": "British Columbia",
        "SK": "Saskatchewan",
        "GENERIC": "Other / Generic"
    }
    lease_types = []
    for value, label in Lease.LeaseType.choices:
        province_code = province_lookup.get(value, "GENERIC")
        lease_types.append({
            "value": value,
            "label": label,
            "property_category": Property.PropertyCategory.ROOM if value in ROOM_TYPES else Property.PropertyCategory.COMPLETE_UNIT,
            "province": {
                "code": province_code,
                "name": FULL_PROVINCE_NAMES.get(province_code, "Unknown")
            }
        })
    return Response(lease_types)


class LeaseViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing leases. Supports different views for landlords and tenants.
    """
    permission_classes = [IsLandlordOrTenantMember]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'lease_type', 'is_month_to_month', 'property', 'group']
    search_fields = ['lease_number', 'property__name', 'group__name', 'property__address']
    ordering_fields = ['start_date', 'end_date', 'created_at', 'lease_number']
    ordering = ['-start_date']
    
    def get_serializer_class(self):
        if self.action == 'list':
            return LeaseListSerializer
        return LeaseSerializer
        
    def get_queryset(self):
        user = self.request.user
        
        base_queryset = Lease.objects.select_related(
            'property', 'group', 'landlord__user'
        ).prefetch_related(
            'lease_tenants__tenant__user',
            'lease_tenants__room',
            'additional_documents',
            'payments__tenant__user'
        )
        
        if hasattr(user, 'landlord_profile'):
            return base_queryset.filter(landlord=user.landlord_profile)
        elif hasattr(user, 'tenant_profile'):
            return base_queryset.filter(lease_tenants__tenant=user.tenant_profile).distinct()
            
        return Lease.objects.none()
        
    def perform_create(self, serializer):
        if not hasattr(self.request.user, 'landlord_profile'):
            raise PermissionDenied("Only landlords can create leases.")
        serializer.save(landlord=self.request.user.landlord_profile)
    
    @action(detail=False, methods=['get'])
    def bill_providers(self, request):
        """
        Returns a structured list of common utility bill types, categories,
        and providers by region for use in the lease creation form.
        """
        bill_providers_by_region = {
            "BC": {
                "electricity": {
                    "display_name": "Electricity",
                    "providers": [
                        {"id": "bc_hydro", "name": "BC Hydro"},
                        {"id": "fortis", "name": "FortisBC (Electricity)"},
                        {"id": "saskpower", "name": "SaskPower"},
                        {"id": "other_electricity", "name": "Other Provider"}
                    ]
                },
                "gas": {
                    "display_name": "Natural Gas",
                    "providers": [
                        {"id": "fortis_gas", "name": "FortisBC Gas"},
                        {"id": "pacific_northern", "name": "Pacific Northern Gas"},
                        {"id": "other_gas", "name": "Other Provider"}
                    ]
                },
                "water": {
                    "display_name": "Water",
                    "providers": [
                        {"id": "city_utilities", "name": "City Utilities"},
                        {"id": "saanich", "name": "Saanich Utilities"},
                        {"id": "crd", "name": "CRD Water"},
                        {"id": "other_water", "name": "Other Provider"}
                    ]
                },
                "heat": {
                    "display_name": "Heating",
                    "providers": [
                        {"id": "bc_hydro_heat", "name": "BC Hydro (Electric Heat)"},
                        {"id": "fortis_heat", "name": "FortisBC (Gas Heat)"},
                        {"id": "building_heat", "name": "Building Managed"},
                        {"id": "other_heat", "name": "Other Provider"}
                    ]
                },
                "internet": {
                    "display_name": "Internet",
                    "providers": [
                        {"id": "telus", "name": "Telus"},
                        {"id": "shaw", "name": "Shaw"},
                        {"id": "rogers", "name": "Rogers"},
                        {"id": "bell", "name": "Bell"},
                        {"id": "other_internet", "name": "Other Provider"}
                    ]
                },
                "waste": {
                    "display_name": "Waste Collection",
                    "providers": [
                        {"id": "city_waste", "name": "City Waste Collection"},
                        {"id": "building_waste", "name": "Building Managed"},
                        {"id": "other_waste", "name": "Other Provider"}
                    ]
                }
            },
            "SK": {
                "electricity": {
                    "display_name": "Electricity",
                    "providers": [
                        {"id": "saskpower", "name": "SaskPower"},
                        {"id": "other_electricity", "name": "Other Provider"}
                    ]
                },
                "gas": {
                    "display_name": "Natural Gas",
                    "providers": [
                        {"id": "saskenergy", "name": "SaskEnergy"},
                        {"id": "other_gas", "name": "Other Provider"}
                    ]
                },
                "water": {
                    "display_name": "Water",
                    "providers": [
                        {"id": "city_utilities", "name": "City Utilities"},
                        {"id": "saskatoon_water", "name": "Saskatoon Water"},
                        {"id": "other_water", "name": "Other Provider"}
                    ]
                },
                "heat": {
                    "display_name": "Heating",
                    "providers": [
                        {"id": "saskpower_heat", "name": "SaskPower (Electric Heat)"},
                        {"id": "saskenergy_heat", "name": "SaskEnergy (Gas Heat)"},
                        {"id": "building_heat", "name": "Building Managed"},
                        {"id": "other_heat", "name": "Other Provider"}
                    ]
                },
                "internet": {
                    "display_name": "Internet",
                    "providers": [
                        {"id": "sasktel", "name": "SaskTel"},
                        {"id": "shaw", "name": "Shaw"},
                        {"id": "access", "name": "Access Communications"},
                        {"id": "other_internet", "name": "Other Provider"}
                    ]
                },
                "waste": {
                    "display_name": "Waste Collection",
                    "providers": [
                        {"id": "city_waste", "name": "City Waste Collection"},
                        {"id": "building_waste", "name": "Building Managed"},
                        {"id": "other_waste", "name": "Other Provider"}
                    ]
                }
            },
            "GENERIC": {
                "electricity": {
                    "display_name": "Electricity",
                    "providers": [
                        {"id": "local_electric", "name": "Local Electric Utility"},
                        {"id": "other_electricity", "name": "Other Provider"}
                    ]
                },
                "gas": {
                    "display_name": "Natural Gas",
                    "providers": [
                        {"id": "local_gas", "name": "Local Gas Utility"},
                        {"id": "other_gas", "name": "Other Provider"}
                    ]
                },
                "water": {
                    "display_name": "Water",
                    "providers": [
                        {"id": "city_water", "name": "City/Municipal Water"},
                        {"id": "other_water", "name": "Other Provider"}
                    ]
                },
                "heat": {
                    "display_name": "Heating",
                    "providers": [
                        {"id": "electric_heat", "name": "Electric Heat"},
                        {"id": "gas_heat", "name": "Gas Heat"},
                        {"id": "oil_heat", "name": "Oil Heat"},
                        {"id": "building_heat", "name": "Building Managed"},
                        {"id": "other_heat", "name": "Other Provider"}
                    ]
                },
                "internet": {
                    "display_name": "Internet",
                    "providers": [
                        {"id": "local_isp", "name": "Local ISP"},
                        {"id": "national_isp", "name": "National Provider"},
                        {"id": "other_internet", "name": "Other Provider"}
                    ]
                },
                "waste": {
                    "display_name": "Waste Collection",
                    "providers": [
                        {"id": "city_waste", "name": "City/Municipal Waste Collection"},
                        {"id": "building_waste", "name": "Building Managed"},
                        {"id": "other_waste", "name": "Other Provider"}
                    ]
                }
            }
        }
        
        # Add responsibility types and distribution methods
        responsibility_types = [
            {"id": "none", "name": "None - Included in Rent"},
            {"id": "full", "name": "Full - Tenant Pays 100%"},
            {"id": "percentage", "name": "Percentage - Tenant Pays a Portion"},
            {"id": "fixed", "name": "Fixed Amount - Tenant Pays Set Fee"}
        ]
        
        distribution_methods = [
            {"id": "none", "name": "None (Not Applicable)"},
            {"id": "equal", "name": "Equal Split Among All Tenants"},
            {"id": "weighted", "name": "Weighted by Rent Amount"},
            {"id": "custom", "name": "Custom Percentages per Tenant"}
        ]
        
        return Response({
            "bill_providers": bill_providers_by_region,
            "responsibility_types": responsibility_types,
            "distribution_methods": distribution_methods
        })

    @action(detail=True, methods=['get'])
    def calculate_bill_share(self, request, pk=None):
        """
        Calculate a tenant's share of a specific utility bill.
        
        Query parameters:
        - tenant_id: UUID of the tenant
        - bill_type: Type of bill (electricity, water, etc.)
        - amount: Total bill amount
        """
        lease = self.get_object()
        
        # Validate parameters
        tenant_id = request.query_params.get('tenant_id')
        bill_type = request.query_params.get('bill_type')
        amount_str = request.query_params.get('amount')
        
        if not tenant_id or not bill_type or not amount_str:
            return Response(
                {"error": "Missing required parameters. Please provide tenant_id, bill_type, and amount."},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        try:
            amount = Decimal(amount_str)
            if amount < 0:
                return Response(
                    {"error": "Bill amount cannot be negative."},
                    status=status.HTTP_400_BAD_REQUEST
                )
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid amount format. Please provide a valid number."},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Check if tenant is associated with this lease
        tenant_lease = get_object_or_404(lease.lease_tenants, tenant__id=tenant_id)
        
        # Check if bill type exists
        if bill_type not in lease.bills_included:
            return Response(
                {"error": f"Bill type '{bill_type}' not found in this lease."},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Calculate tenant's share
        tenant_share = lease.calculate_tenant_bill_share(tenant_id, bill_type, amount)
        bill_details = lease.bills_included.get(bill_type, {})
        
        # Generate human-readable calculation explanation
        calculation_details = self._generate_calculation_explanation(
            lease, tenant_id, bill_type, amount, tenant_share
        )
        
        return Response({
            "tenant_id": tenant_id,
            "tenant_name": tenant_lease.tenant.user.name,
            "lease_number": lease.lease_number,
            "bill_type": bill_type,
            "bill_provider": bill_details.get('provider', ''),
            "total_amount": float(amount),
            "tenant_share": float(tenant_share),
            "calculation_details": calculation_details
        })
        
    def _generate_calculation_explanation(self, lease, tenant_id, bill_type, amount, tenant_share):
        """Generate a human-readable explanation of how the bill share was calculated."""
        bill_details = lease.bills_included.get(bill_type, {})
        if not bill_details:
            return {"explanation": "Bill details not found"}
            
        # Get tenant count for distribution calculations
        tenant_count = lease.lease_tenants.count()
        
        if bill_details.get('included', False):
            return {
                "responsibility_type": "none",
                "explanation": "Bill is included in rent. No additional payment required."
            }
            
        resp = bill_details.get('tenant_responsibility', {})
        resp_type = resp.get('type')
        distribution = resp.get('distribution')
        
        if resp_type == 'none':
            return {
                "responsibility_type": "none",
                "explanation": "No tenant responsibility for this bill."
            }
            
        explanation = ""
        
        if resp_type == 'full':
            value = 100
            explanation = f"Tenant responsible for full bill"
        elif resp_type == 'percentage':
            value = resp.get('value', 0)
            explanation = f"Tenant responsible for {value}% of bill"
        elif resp_type == 'fixed':
            value = resp.get('value', 0)
            explanation = f"Tenant pays fixed amount of ${value}"
            if distribution == 'equal':
                explanation += f" divided equally among {tenant_count} tenants"
            return {
                "responsibility_type": resp_type,
                "responsibility_value": value,
                "distribution_method": distribution,
                "tenant_count": tenant_count,
                "explanation": explanation,
                "calculation": f"Fixed amount: ${value}" + 
                              (f" ÷ {tenant_count} tenants = ${float(tenant_share)}" 
                               if distribution == 'equal' and tenant_count > 1 else "")
            }
            
        # Add distribution explanation
        if distribution == 'equal' and tenant_count > 1:
            explanation += f", divided equally among {tenant_count} tenants"
            calculation = f"${float(amount)} × {value}% ÷ {tenant_count} tenants = ${float(tenant_share)}"
        elif distribution == 'custom':
            custom_splits = resp.get('custom_splits', {})
            tenant_percentage = custom_splits.get(str(tenant_id), 0)
            explanation += f", with custom split of {tenant_percentage}% for this tenant"
            calculation = f"${float(amount)} × {value}% × {tenant_percentage}% = ${float(tenant_share)}"
        elif distribution == 'weighted':
            explanation += ", weighted by each tenant's rent amount"
            try:
                tenant_lease = lease.lease_tenants.get(tenant__id=tenant_id)
                tenant_rent = tenant_lease.rent_amount
                total_rent = lease.get_total_monthly_rent()
                tenant_weight = (tenant_rent / total_rent) * 100 if total_rent > 0 else 0
                calculation = f"${float(amount)} × {value}% × {tenant_weight:.1f}% rent weight = ${float(tenant_share)}"
            except:
                calculation = f"${float(amount)} × {value}% with rent-weighted distribution = ${float(tenant_share)}"
        else:
            calculation = f"${float(amount)} × {value}% = ${float(tenant_share)}"
            
        return {
            "responsibility_type": resp_type,
            "responsibility_value": value,
            "distribution_method": distribution,
            "tenant_count": tenant_count,
            "explanation": explanation,
            "calculation": calculation
        }
    
    @action(detail=True, methods=['get'])
    def all_bill_shares(self, request, pk=None):
        """
        Calculate all tenants' shares for each bill type with a given amount.
        
        Query parameters:
        - bill_amounts: JSON object with bill types as keys and amounts as values
          e.g., {"electricity": 150, "gas": 80}
        """
        lease = self.get_object()
        
        try:
            bill_amounts = json.loads(request.query_params.get('bill_amounts', '{}'))
        except json.JSONDecodeError:
            return Response(
                {"error": "Invalid bill_amounts format. Please provide a valid JSON object."},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        if not bill_amounts:
            return Response(
                {"error": "No bill amounts provided."},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        results = {}
        for bill_type, amount in bill_amounts.items():
            if bill_type not in lease.bills_included:
                continue
                
            bill_results = {}
            for lease_tenant in lease.lease_tenants.all():
                tenant_id = str(lease_tenant.tenant.id)
                tenant_share = lease.calculate_tenant_bill_share(tenant_id, bill_type, Decimal(amount))
                bill_results[tenant_id] = {
                    "tenant_name": lease_tenant.tenant.user.name,
                    "share_amount": float(tenant_share)
                }
                
            results[bill_type] = {
                "total_amount": float(amount),
                "provider": lease.bills_included[bill_type].get('provider', ''),
                "tenant_shares": bill_results
            }
            
        return Response(results)
        
    @action(detail=True, methods=['post'], permission_classes=[IsLandlordOwner])
    def create_utility_payment(self, request, pk=None):
        """
        Create utility bill payments for tenants based on their calculated shares.
        
        Required fields in request data:
        - bill_type: Type of utility (electricity, water, etc.)
        - total_amount: Total bill amount
        - utility_provider: Name of the utility provider
        - due_date: When payment is due (YYYY-MM-DD)
        - period_start: Start date of billing period (YYYY-MM-DD)
        - period_end: End date of billing period (YYYY-MM-DD)
        - tenant_ids: List of tenant IDs to create payments for, or empty for all tenants
        """
        lease = self.get_object()
        
        # Validate request data
        required_fields = ['bill_type', 'total_amount', 'utility_provider', 'due_date', 'period_start', 'period_end']
        missing_fields = [field for field in required_fields if field not in request.data]
        if missing_fields:
            return Response(
                {"error": f"Missing required fields: {', '.join(missing_fields)}"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        bill_type = request.data['bill_type']
        utility_provider = request.data['utility_provider']
        
        try:
            total_amount = Decimal(request.data['total_amount'])
            due_date = timezone.datetime.strptime(request.data['due_date'], '%Y-%m-%d').date()
            period_start = timezone.datetime.strptime(request.data['period_start'], '%Y-%m-%d').date()
            period_end = timezone.datetime.strptime(request.data['period_end'], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid date or amount format. Use YYYY-MM-DD for dates."},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Check if bill exists in lease
        if bill_type not in lease.bills_included:
            return Response(
                {"error": f"Bill type '{bill_type}' not found in this lease."},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Determine which tenants to create payments for
        tenant_ids = request.data.get('tenant_ids', [])
        if tenant_ids:
            tenant_leases = lease.lease_tenants.filter(tenant__id__in=tenant_ids)
        else:
            tenant_leases = lease.lease_tenants.all()
            
        if not tenant_leases.exists():
            return Response(
                {"error": "No valid tenants found for creating payments."},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        created_payments = []
        
        # Create a payment for each tenant based on their share
        for tenant_lease in tenant_leases:
            tenant_id = str(tenant_lease.tenant.id)
            tenant_share = lease.calculate_tenant_bill_share(tenant_id, bill_type, total_amount)
            
            # Skip if tenant has no share to pay
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
                notes=f"Utility payment for {utility_provider} ({bill_type}) - {period_start} to {period_end}"
            )
            
            payment_data = PaymentSerializer(payment).data
            created_payments.append(payment_data)
            
        return Response({
            "message": f"Created {len(created_payments)} utility payments.",
            "payments": created_payments
        }, status=status.HTTP_201_CREATED)
        
    @action(detail=True, methods=['post'])
    def terminate(self, request, pk=None):
        """Terminate an active or pending lease."""
        lease = self.get_object()
        
        if lease.status in [Lease.LeaseStatus.TERMINATED, Lease.LeaseStatus.EXPIRED, Lease.LeaseStatus.RENEWED]:
            raise ValidationError(f"Lease is already in a final state ({lease.status}).")
            
        termination_date_str = request.data.get('termination_date')
        move_out_date_str = request.data.get('move_out_date', termination_date_str)
        
        try:
            termination_date = timezone.datetime.strptime(termination_date_str, '%Y-%m-%d').date() if termination_date_str else timezone.now().date()
            move_out_date = timezone.datetime.strptime(move_out_date_str, '%Y-%m-%d').date() if move_out_date_str else termination_date
        except (ValueError, TypeError):
            raise ValidationError("Invalid date format. Use YYYY-MM-DD.")
            
        lease.status = Lease.LeaseStatus.TERMINATED
        lease.move_out_date = move_out_date
        
        # Set end_date if not already set or if termination is earlier
        if not lease.end_date or lease.end_date > termination_date:
            lease.end_date = termination_date
            
        lease.save()
        serializer = self.get_serializer(lease)
        return Response(serializer.data)
        
    @action(detail=True, methods=['post'], permission_classes=[IsLandlordOwner])
    def renew(self, request, pk=None):
        """Renew a lease by creating a new lease linked to the old one."""
        old_lease = self.get_object()
        
        if old_lease.status in [Lease.LeaseStatus.DRAFT, Lease.LeaseStatus.PENDING_SIGNATURES]:
            raise ValidationError("Cannot renew a lease that is not yet active or finalized.")
            
        # Create the new lease
        serializer = LeaseSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        
        # Mark the old lease as renewed
        old_lease.status = Lease.LeaseStatus.RENEWED
        old_lease.save()
        
        # Create the new lease linked to the old one
        new_lease = serializer.save(
            landlord=request.user.landlord_profile,
            previous_lease=old_lease
        )
        
        # Optionally copy tenants from old lease
        if request.data.get('copy_tenants', True):
            for old_lease_tenant in old_lease.lease_tenants.all():
                # Skip if tenant already added via serializer data
                if not new_lease.lease_tenants.filter(tenant=old_lease_tenant.tenant).exists():
                    LeaseTenant.objects.create(
                        lease=new_lease,
                        tenant=old_lease_tenant.tenant,
                        rent_amount=old_lease_tenant.rent_amount,
                        room=old_lease_tenant.room,
                        is_primary_tenant=old_lease_tenant.is_primary_tenant,
                        cleaning_fee=old_lease_tenant.cleaning_fee,
                        cleaning_fee_paid=False,  # Reset payment status
                        has_signed=False,  # Reset signing status
                    )
                    
        new_lease_serializer = self.get_serializer(new_lease)
        return Response(new_lease_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], permission_classes=[IsLandlordOwner])
    def available_tenants(self, request):
        """Get all tenants that can be added to leases."""
        tenants = TenantProfile.objects.all().select_related('user')
        serializer = TenantBasicSerializer(tenants, many=True)
        return Response(serializer.data)


class LeaseTenantViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing tenant associations with leases.
    """
    serializer_class = LeaseTenantSerializer
    permission_classes = [IsLandlordOrTenantMember]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['lease', 'tenant', 'room', 'has_signed', 'cleaning_fee_paid']
    ordering_fields = ['rent_amount', 'signed_date', 'created_at']
    ordering = ['lease', 'tenant__user__name']
    
    def get_queryset(self):
        user = self.request.user
        
        base_queryset = LeaseTenant.objects.select_related('tenant__user', 'lease', 'room')
        
        if hasattr(user, 'landlord_profile'):
            return base_queryset.filter(lease__landlord=user.landlord_profile)
        elif hasattr(user, 'tenant_profile'):
            return base_queryset.filter(tenant=user.tenant_profile)
            
        return LeaseTenant.objects.none()
        
    def get_serializer_context(self):
        """Add lease to context if available."""
        context = super().get_serializer_context()
        
        if 'lease_pk' in self.kwargs:
            try:
                lease = get_object_or_404(Lease, pk=self.kwargs['lease_pk'])
                self.check_object_permissions(self.request, lease)
                context['lease'] = lease
            except:
                pass
                
        return context
        
    def perform_create(self, serializer):
        user = self.request.user
        
        if not hasattr(user, 'landlord_profile'):
            raise PermissionDenied("Only landlords can add tenants to a lease.")
            
        lease = serializer.validated_data.get('lease')
        if not lease:
            lease = self.get_serializer_context().get('lease')
            if not lease:
                raise ValidationError("Lease must be specified.")
                
        if lease.landlord != user.landlord_profile:
            raise PermissionDenied("You can only add tenants to your own leases.")
            
        serializer.save(lease=lease)
        
        # Update lease status if needed
        if lease.status == Lease.LeaseStatus.DRAFT and lease.lease_tenants.count() > 0:
            lease.status = Lease.LeaseStatus.PENDING_SIGNATURES
            lease.save(update_fields=['status'])
            
    @action(detail=True, methods=['post'])
    def sign(self, request, pk=None):
        """Mark a lease as signed by the tenant."""
        lease_tenant = self.get_object()
        
        # Only the tenant themselves can sign
        if not hasattr(request.user, 'tenant_profile') or lease_tenant.tenant != request.user.tenant_profile:
            raise PermissionDenied("You can only sign your own lease agreement.")
            
        if lease_tenant.has_signed:
            raise ValidationError("This agreement is already signed.")
            
        lease_tenant.has_signed = True
        lease_tenant.signed_date = timezone.now()
        lease_tenant.save()
        
        # Check if all tenants have signed and update lease status if needed
        lease = lease_tenant.lease
        if lease.status == Lease.LeaseStatus.PENDING_SIGNATURES:
            all_signed = not lease.lease_tenants.filter(has_signed=False).exists()
            if all_signed:
                lease.status = Lease.LeaseStatus.ACTIVE
                lease.save(update_fields=['status'])
                
        serializer = self.get_serializer(lease_tenant)
        return Response(serializer.data)
        
    @action(detail=True, methods=['post'], permission_classes=[IsLandlordOwner])
    def mark_cleaning_fee_paid(self, request, pk=None):
        """Mark the cleaning fee as paid for a tenant."""
        lease_tenant = self.get_object()
        
        if lease_tenant.cleaning_fee_paid:
            raise ValidationError("Cleaning fee is already marked as paid.")
            
        if lease_tenant.cleaning_fee <= 0:
            raise ValidationError("No cleaning fee was set for this tenant.")
            
        lease_tenant.cleaning_fee_paid = True
        lease_tenant.save()
        
        serializer = self.get_serializer(lease_tenant)
        return Response(serializer.data)


class LeaseDocumentViewSet(viewsets.ModelViewSet):
    """ViewSet for managing lease documents."""
    serializer_class = LeaseDocumentSerializer
    permission_classes = [IsLandlordOrTenantMember]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['lease', 'is_signed']
    search_fields = ['title', 'description']
    
    def get_queryset(self):
        user = self.request.user
        
        base_queryset = LeaseDocument.objects.select_related('lease')
        
        if hasattr(user, 'landlord_profile'):
            return base_queryset.filter(lease__landlord=user.landlord_profile)
        elif hasattr(user, 'tenant_profile'):
            return base_queryset.filter(lease__lease_tenants__tenant=user.tenant_profile).distinct()
            
        return LeaseDocument.objects.none()
        
    def get_serializer_context(self):
        context = super().get_serializer_context()
        
        if 'lease_pk' in self.kwargs:
            try:
                lease = get_object_or_404(Lease, pk=self.kwargs['lease_pk'])
                self.check_object_permissions(self.request, lease)
                context['lease'] = lease
            except:
                pass
                
        return context
        
    def perform_create(self, serializer):
        user = self.request.user
        
        if not hasattr(user, 'landlord_profile'):
            raise PermissionDenied("Only landlords can upload lease documents.")
            
        lease = serializer.validated_data.get('lease')
        if not lease:
            lease = self.get_serializer_context().get('lease')
            if not lease:
                raise ValidationError("Lease must be specified.")
                
        if lease.landlord != user.landlord_profile:
            raise PermissionDenied("You can only upload documents to your own leases.")
            
        serializer.save(lease=lease)


class PaymentViewSet(viewsets.ModelViewSet):
    """ViewSet for managing payments."""
    serializer_class = PaymentSerializer
    permission_classes = [IsLandlordOrTenantMember]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {
        'lease': ['exact'],
        'tenant': ['exact'],
        'payment_type': ['exact', 'in'],
        'status': ['exact', 'in'],
        'due_date': ['exact', 'gte', 'lte'],
        'payment_date': ['exact', 'gte', 'lte', 'isnull'],
        'utility_type': ['exact', 'isnull'],
    }
    search_fields = ['reference_number', 'notes', 'utility_provider']
    ordering_fields = ['due_date', 'payment_date', 'amount_due', 'created_at']
    ordering = ['due_date']
    
    def get_queryset(self):
        user = self.request.user
        
        base_queryset = Payment.objects.select_related('lease', 'tenant__user').prefetch_related('reminders')
        
        if hasattr(user, 'landlord_profile'):
            return base_queryset.filter(lease__landlord=user.landlord_profile)
        elif hasattr(user, 'tenant_profile'):
            return base_queryset.filter(tenant=user.tenant_profile)
            
        return Payment.objects.none()
        
    def get_serializer_context(self):
        context = super().get_serializer_context()
        
        if 'lease_pk' in self.kwargs:
            try:
                lease = get_object_or_404(Lease, pk=self.kwargs['lease_pk'])
                self.check_object_permissions(self.request, lease)
                context['lease'] = lease
            except:
                pass
                
        return context
        
    def perform_create(self, serializer):
        user = self.request.user
        
        if not hasattr(user, 'landlord_profile'):
            raise PermissionDenied("Only landlords can record payments.")
            
        lease = serializer.validated_data.get('lease')
        if not lease:
            lease = self.get_serializer_context().get('lease')
            if not lease:
                raise ValidationError("Lease must be specified.")
                
        if lease.landlord != user.landlord_profile:
            raise PermissionDenied("You can only record payments for your own leases.")
            
        serializer.save(lease=lease)
        
    @action(detail=True, methods=['post'], permission_classes=[IsLandlordOwner])
    def mark_as_paid(self, request, pk=None):
        """Mark a payment as completed."""
        payment = self.get_object()
        
        if payment.status == Payment.PaymentStatus.COMPLETED:
            raise ValidationError("Payment is already marked as completed.")
            
        payment.amount_paid = payment.amount_due  # Mark as fully paid
        payment.payment_date = request.data.get('payment_date') or timezone.now().date()
        payment.payment_method = request.data.get('payment_method') or payment.payment_method or Payment.PaymentMethod.OTHER
        payment.reference_number = request.data.get('reference_number', payment.reference_number)
        payment.notes = request.data.get('notes', payment.notes)
        
        # Save will automatically update status
        payment.save()
        
        serializer = self.get_serializer(payment)
        return Response(serializer.data)
        
    @action(detail=True, methods=['post'], permission_classes=[IsLandlordOwner])
    def refund(self, request, pk=None):
        """Mark a payment as refunded."""
        payment = self.get_object()
        
        if payment.status not in [Payment.PaymentStatus.COMPLETED, Payment.PaymentStatus.PARTIALLY_PAID]:
            raise ValidationError("Can only refund payments that were completed or partially paid.")
            
        payment.status = Payment.PaymentStatus.REFUNDED
        refund_notes = request.data.get('notes', "Payment Refunded")
        payment.notes += f"\n---\nRefunded on {timezone.now().date()}. Reason: {refund_notes}"
        payment.save()
        
        serializer = self.get_serializer(payment)
        return Response(serializer.data)


class PaymentReminderViewSet(viewsets.ModelViewSet):
    """ViewSet for managing payment reminders."""
    serializer_class = PaymentReminderSerializer
    permission_classes = [IsLandlordOwner]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['payment', 'is_sent', 'reminder_date']
    ordering_fields = ['reminder_date', 'sent_date']
    ordering = ['reminder_date']
    
    def get_queryset(self):
        user = self.request.user
        
        if hasattr(user, 'landlord_profile'):
            return PaymentReminder.objects.filter(
                payment__lease__landlord=user.landlord_profile
            ).select_related('payment')
            
        return PaymentReminder.objects.none()
        
    def get_serializer_context(self):
        context = super().get_serializer_context()
        
        if 'payment_pk' in self.kwargs:
            try:
                payment = get_object_or_404(Payment, pk=self.kwargs['payment_pk'])
                self.check_object_permissions(self.request, payment)
                context['payment'] = payment
            except:
                pass
                
        return context
        
    def perform_create(self, serializer):
        user = self.request.user
        
        payment = serializer.validated_data.get('payment')
        if not payment:
            payment = self.get_serializer_context().get('payment')
            if not payment:
                raise ValidationError("Payment must be specified.")
                
        if payment.lease.landlord != user.landlord_profile:
            raise PermissionDenied("Cannot create reminders for payments not belonging to your leases.")
            
        serializer.save(payment=payment)
        
    @action(detail=True, methods=['post'])
    def mark_as_sent(self, request, pk=None):
        """Mark a reminder as sent."""
        reminder = self.get_object()
        
        if reminder.is_sent:
            raise ValidationError("Reminder is already marked as sent.")
            
        reminder.is_sent = True
        reminder.sent_date = timezone.now()
        reminder.save()
        
        serializer = self.get_serializer(reminder)
        return Response(serializer.data)