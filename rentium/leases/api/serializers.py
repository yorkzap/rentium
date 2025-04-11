from rest_framework import serializers
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rentium.leases.models import (
    Lease, LeaseTenant, LeaseDocument, Payment, PaymentReminder
)
from rentium.users.models import TenantProfile
from rentium.properties.models import Property, PropertyGroup


class TenantBasicSerializer(serializers.ModelSerializer):
    """Basic serializer for tenant information"""
    name = serializers.CharField(source='user.name', read_only=True)
    email = serializers.CharField(source='user.email', read_only=True)
    
    class Meta:
        model = TenantProfile
        fields = ['id', 'name', 'email']


class LeaseTenantSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source='tenant.user.name', read_only=True)
    tenant_email = serializers.CharField(source='tenant.user.email', read_only=True)
    room_name = serializers.CharField(source='room.name', read_only=True, allow_null=True)
    
    # Writable fields for linking
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=TenantProfile.objects.all(), source='tenant', write_only=True
    )
    room_id = serializers.PrimaryKeyRelatedField(
        queryset=Property.objects.filter(property_category=Property.PropertyCategory.ROOM),
        source='room', write_only=True, required=False, allow_null=True
    )
    
    class Meta:
        model = LeaseTenant
        fields = [
            'id', 'lease',
            'tenant_id', 'tenant_name', 'tenant_email',
            'rent_amount',
            'room_id', 'room_name',
            'cleaning_fee', 'cleaning_fee_paid',
            'is_primary_tenant', 'has_signed', 'signed_date',
            'individual_start_date', 'individual_end_date',
            'tenant_notes',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'lease', 'tenant_name', 'tenant_email', 'room_name',
            'has_signed', 'signed_date',
            'cleaning_fee_paid',
            'created_at', 'updated_at'
        ]
        extra_kwargs = {
            'lease': {'required': False, 'write_only': True}
        }
        
    def validate(self, data):
        # Get the lease from context or data
        lease = self.context.get('lease') or data.get('lease')
        if not lease:
            raise serializers.ValidationError("Lease context is required.")
            
        room = data.get('room')
        tenant = data.get('tenant')
        
        # Check for duplicate tenant on the same lease (if creating)
        instance = getattr(self, 'instance', None)
        if not instance and LeaseTenant.objects.filter(lease=lease, tenant=tenant).exists():
            raise serializers.ValidationError({
                'tenant_id': f"Tenant {tenant.user.name} is already associated with this lease."
            })
            
        # Validate using model's clean method
        try:
            temp_instance = instance or LeaseTenant(lease=lease)
            for key, value in data.items():
                setattr(temp_instance, key, value)
            temp_instance.clean()
        except DjangoValidationError as e:
            raise serializers.ValidationError(serializers.as_serializer_error(e))
            
        return data


class LeaseDocumentSerializer(serializers.ModelSerializer):
    lease_number = serializers.CharField(source='lease.lease_number', read_only=True)
    
    class Meta:
        model = LeaseDocument
        fields = [
            'id', 'lease', 'lease_number', 'title', 'document',
            'description', 'is_signed', 'uploaded_at'
        ]
        read_only_fields = ['id', 'lease_number', 'uploaded_at']
        extra_kwargs = {
            'lease': {'required': False, 'write_only': True}
        }


class PaymentReminderSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentReminder
        fields = [
            'id', 'payment', 'reminder_date', 'message_template',
            'is_sent', 'sent_date', 'send_method',
            'error_message', 'created_at'
        ]
        read_only_fields = [
            'id', 'is_sent', 'sent_date', 'error_message', 'created_at'
        ]
        extra_kwargs = {
            'payment': {'required': False}
        }


class PaymentSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source='tenant.user.name', read_only=True)
    payment_type_display = serializers.CharField(source='get_payment_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    payment_method_display = serializers.CharField(source='get_payment_method_display', read_only=True, allow_null=True)
    
    # Writable tenant ID
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=TenantProfile.objects.all(), source='tenant', write_only=True
    )
    
    reminders = PaymentReminderSerializer(many=True, read_only=True)
    
    class Meta:
        model = Payment
        fields = [
            'id', 'lease',
            'tenant_id', 'tenant_name',
            'payment_type', 'payment_type_display',
            'amount_due', 'amount_paid',
            'due_date', 'payment_date',
            'status', 'status_display',
            'payment_method', 'payment_method_display',
            'reference_number', 'notes', 'receipt_file',
            'utility_type', 'utility_provider', 
            'utility_period_start', 'utility_period_end',
            'reminders', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'tenant_name',
            'payment_type_display', 'status_display', 'payment_method_display',
            'reminders', 'created_at', 'updated_at'
        ]
        extra_kwargs = {
            'lease': {'required': False, 'write_only': True},
            'amount_paid': {'allow_null': True},
            'payment_date': {'allow_null': True},
            'payment_method': {'allow_null': True},
            'utility_period_start': {'allow_null': True},
            'utility_period_end': {'allow_null': True},
        }
        
    def validate(self, data):
        # Basic validation
        if data.get('amount_paid', 0) is not None and data.get('amount_paid', 0) < 0:
            raise serializers.ValidationError({'amount_paid': "Amount paid cannot be negative."})
        if data.get('amount_due', 0) < 0:
            raise serializers.ValidationError({'amount_due': "Amount due cannot be negative."})
            
        # Ensure tenant belongs to the lease
        lease = self.context.get('lease') or data.get('lease')
        tenant = data.get('tenant')
        if lease and tenant and not lease.lease_tenants.filter(tenant=tenant).exists():
            raise serializers.ValidationError({
                'tenant_id': f"Tenant {tenant.user.name} is not associated with lease {lease.lease_number}."
            })
        
        # If utility payment, validate additional fields
        payment_type = data.get('payment_type')
        if payment_type == Payment.PaymentType.UTILITY:
            if not data.get('utility_type'):
                raise serializers.ValidationError({'utility_type': "Utility type is required for utility payments."})
            if not data.get('utility_provider'):
                raise serializers.ValidationError({'utility_provider': "Utility provider is required for utility payments."})
                
        return data


class UtilityBillSerializer(serializers.Serializer):
    """Serializer for utility bill calculation requests."""
    bill_type = serializers.CharField(required=True)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=True)
    tenant_id = serializers.UUIDField(required=True)
    
    def validate_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("Bill amount cannot be negative.")
        return value


class LeaseListSerializer(serializers.ModelSerializer):
    """Simplified serializer for list views"""
    lease_type_display = serializers.CharField(source='get_lease_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    property_name = serializers.CharField(source='property.name', read_only=True, allow_null=True)
    property_address = serializers.CharField(source='property.address', read_only=True, allow_null=True)
    group_name = serializers.CharField(source='group.name', read_only=True, allow_null=True)
    tenant_count = serializers.IntegerField(source='get_current_tenant_count', read_only=True)
    total_rent = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, source='get_total_monthly_rent')
    landlord_name = serializers.CharField(source='landlord.user.name', read_only=True)
    
    class Meta:
        model = Lease
        fields = [
            'id', 'lease_number', 'lease_type', 'lease_type_display', 'status', 'status_display', 
            'property_name', 'property_address', 'group_name', 'landlord_name',
            'start_date', 'end_date', 'is_month_to_month', 
            'tenant_count', 'total_rent', 'created_at'
        ]


class LeaseSerializer(serializers.ModelSerializer):
    lease_type_display = serializers.CharField(source='get_lease_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    landlord_name = serializers.CharField(source='landlord.user.name', read_only=True)
    property_name = serializers.CharField(source='property.name', read_only=True, allow_null=True)
    property_address = serializers.CharField(source='property.address', read_only=True, allow_null=True)
    group_name = serializers.CharField(source='group.name', read_only=True, allow_null=True)
    bills_summary = serializers.CharField(source='get_bills_summary', read_only=True)
    
    # Nested resources
    lease_tenants = LeaseTenantSerializer(many=True, read_only=True)
    additional_documents = LeaseDocumentSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    
    # Calculated fields
    total_monthly_rent = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, source='get_total_monthly_rent')
    current_tenant_count = serializers.IntegerField(read_only=True, source='get_current_tenant_count')
    max_occupancy = serializers.IntegerField(read_only=True, source='get_max_occupancy')
    
    # For linking property/group on create/update
    property_id = serializers.PrimaryKeyRelatedField(
        queryset=Property.objects.all(), source='property', write_only=True, required=False, allow_null=True
    )
    group_id = serializers.PrimaryKeyRelatedField(
        queryset=PropertyGroup.objects.all(), source='group', write_only=True, required=False, allow_null=True
    )
    
    class Meta:
        model = Lease
        fields = [
            'id', 'lease_type', 'lease_type_display',
            'property_id', 'property_name', 'property_address',
            'group_id', 'group_name',
            'landlord', 'landlord_name',
            'lease_number', 'status', 'status_display',
            'start_date', 'end_date', 'is_month_to_month',
            'move_in_date', 'move_out_date',
            'security_deposit', 'pet_deposit', 'cleaning_fee',
            'pets_allowed', 'smoking_allowed', 'bills_included', 'bills_summary', 'special_terms',
            'document_file',
            'previous_lease',
            'created_at', 'updated_at',
            'lease_tenants', 'additional_documents', 'payments',
            'total_monthly_rent', 'current_tenant_count', 'max_occupancy'
        ]
        read_only_fields = [
            'id', 'landlord', 'landlord_name', 'lease_number',
            'lease_type_display', 'status_display',
            'property_name', 'property_address', 'group_name',
            'created_at', 'updated_at',
            'lease_tenants', 'additional_documents', 'payments',
            'total_monthly_rent', 'current_tenant_count', 'max_occupancy'
        ]
        extra_kwargs = {
            'landlord': {'read_only': True},
            'previous_lease': {'allow_null': True},
        }
    
    def validate_bills_included(self, bills_included):
        """Validate the structure of bills_included field."""
        if not bills_included:
            return bills_included
            
        VALID_BILL_CATEGORIES = {
            'electricity', 'water', 'gas', 'internet', 'waste', 'heat', 'cable', 'sewer'
        }
        
        VALID_RESPONSIBILITY_TYPES = {'none', 'percentage', 'fixed', 'full'}
        VALID_DISTRIBUTION_TYPES = {'none', 'equal', 'weighted', 'custom'}
        
        for bill_key, bill_data in bills_included.items():
            # Check required fields
            if not isinstance(bill_data, dict):
                raise serializers.ValidationError(f"Bill {bill_key} must be an object")
            
            required_fields = ['included', 'provider', 'category']
            for field in required_fields:
                if field not in bill_data:
                    raise serializers.ValidationError(f"Bill {bill_key} is missing required field: {field}")
            
            # Check category is valid
            if bill_data['category'] not in VALID_BILL_CATEGORIES:
                raise serializers.ValidationError(
                    f"Invalid category '{bill_data['category']}' for bill {bill_key}. "
                    f"Valid categories are: {', '.join(VALID_BILL_CATEGORIES)}"
                )
            
            # If not included in rent, must have tenant_responsibility
            if not bill_data.get('included', True):
                if 'tenant_responsibility' not in bill_data:
                    raise serializers.ValidationError(
                        f"Bill {bill_key} is not included in rent but missing tenant_responsibility details"
                    )
                
                resp = bill_data['tenant_responsibility']
                if not isinstance(resp, dict):
                    raise serializers.ValidationError(f"tenant_responsibility for {bill_key} must be an object")
                
                # Check responsibility fields
                if 'type' not in resp:
                    raise serializers.ValidationError(f"tenant_responsibility for {bill_key} missing 'type'")
                
                if resp['type'] not in VALID_RESPONSIBILITY_TYPES:
                    raise serializers.ValidationError(
                        f"Invalid responsibility type '{resp['type']}' for {bill_key}. "
                        f"Valid types are: {', '.join(VALID_RESPONSIBILITY_TYPES)}"
                    )
                
                if 'distribution' not in resp:
                    raise serializers.ValidationError(f"tenant_responsibility for {bill_key} missing 'distribution'")
                
                if resp['distribution'] not in VALID_DISTRIBUTION_TYPES:
                    raise serializers.ValidationError(
                        f"Invalid distribution type '{resp['distribution']}' for {bill_key}. "
                        f"Valid types are: {', '.join(VALID_DISTRIBUTION_TYPES)}"
                    )
                
                # Additional validation based on responsibility type
                if resp['type'] != 'none' and ('value' not in resp or not isinstance(resp['value'], (int, float))):
                    raise serializers.ValidationError(f"tenant_responsibility for {bill_key} requires a numeric 'value'")
                
                # Validate percentage is between 0-100
                if resp['type'] == 'percentage' and (resp['value'] < 0 or resp['value'] > 100):
                    raise serializers.ValidationError(f"Percentage value for {bill_key} must be between 0 and 100")
                
                # Validate custom distribution if specified
                if resp['distribution'] == 'custom':
                    if 'custom_splits' not in resp or not isinstance(resp['custom_splits'], dict):
                        raise serializers.ValidationError(
                            f"Custom distribution for {bill_key} requires 'custom_splits' object"
                        )
                    
                    splits_total = sum(resp['custom_splits'].values())
                    if abs(splits_total - 100) > 0.01:  # Allow small floating point differences
                        raise serializers.ValidationError(
                            f"Custom splits for {bill_key} must add up to 100%, got {splits_total}%"
                        )
            
        return bills_included
    
    def validate(self, data):
        # Get related objects
        property_obj = data.get('property', getattr(self.instance, 'property', None))
        group_obj = data.get('group', getattr(self.instance, 'group', None))
        lease_type = data.get('lease_type', getattr(self.instance, 'lease_type', None))
        
        # Validate using model's clean method
        try:
            # Create a temporary instance for validation
            instance_data = {
                **(self.instance.__dict__ if self.instance else {}),
                **data,
                'property': property_obj,
                'group': group_obj,
            }
            # Remove fields not in the model
            model_fields = {f.name for f in Lease._meta.get_fields()}
            cleaned_instance_data = {k: v for k, v in instance_data.items() if k in model_fields}
            
            temp_instance = Lease(**cleaned_instance_data)
            temp_instance.clean()
        except DjangoValidationError as e:
            raise serializers.ValidationError(serializers.as_serializer_error(e))
        except AttributeError:
            # Basic validation if clean method can't run
            if not ((property_obj is None) ^ (group_obj is None)):  # XOR check
                raise serializers.ValidationError("Lease must link to EITHER property OR group.")
        
        return data
    
    @transaction.atomic
    def create(self, validated_data):
        # Create the lease
        lease = super().create(validated_data)
        return lease