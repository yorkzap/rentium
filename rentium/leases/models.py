import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db.models import Sum
from decimal import Decimal
from rentium.properties.models import Property, PropertyGroup
from rentium.users.models import TenantProfile, LandlordProfile


class Lease(models.Model):
    class LeaseType(models.TextChoices):
        # BC agreements
        BC_ROOMMATE_AGREEMENT = "BC_ROOMMATE", _("BC TRAC Roommate Agreement")
        BC_RESIDENTIAL_TENANCY = "BC_RESIDENTIAL", _("BC Residential Tenancy (RTB-1)")
        # Saskatchewan agreements
        SK_ROOMMATE_AGREEMENT = "SK_ROOMMATE", _("Saskatchewan Roommate Agreement")
        SK_RESIDENTIAL_TENANCY = "SK_RESIDENTIAL", _("Saskatchewan Residential Tenancy")
        # Generic agreement (for other provinces)
        GENERIC_ROOMMATE = "GENERIC_ROOMMATE", _("Standard Roommate Agreement")
        GENERIC_RESIDENTIAL = "GENERIC_RESIDENTIAL", _("Standard Residential Agreement")

    class LeaseStatus(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        PENDING_SIGNATURES = "PENDING", _("Pending Signatures")
        ACTIVE = "ACTIVE", _("Active")
        EXPIRED = "EXPIRED", _("Expired")
        TERMINATED = "TERMINATED", _("Terminated")
        RENEWED = "RENEWED", _("Renewed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease_type = models.CharField(_("Lease Type"), max_length=25, choices=LeaseType.choices)
    property = models.ForeignKey(
        Property, 
        on_delete=models.PROTECT, 
        related_name="leases",
        null=True,
        blank=True,
        help_text=_("Link to a specific property (Room or Complete Unit)")
    )
    group = models.ForeignKey(
        PropertyGroup,
        on_delete=models.PROTECT,
        related_name="group_leases",
        null=True,
        blank=True,
        help_text=_("Link to a group for shared accommodation agreements covering multiple rooms")
    )
    landlord = models.ForeignKey(
        LandlordProfile, 
        on_delete=models.PROTECT, 
        related_name="landlord_leases"
    )
    
    # Common lease fields
    lease_number = models.CharField(_("Lease Number"), max_length=20, unique=True, blank=True, editable=False)
    status = models.CharField(_("Status"), max_length=20, choices=LeaseStatus.choices, default=LeaseStatus.DRAFT)
    start_date = models.DateField(_("Start Date"))
    end_date = models.DateField(_("End Date"), null=True, blank=True, help_text=_("Blank for month-to-month"))
    is_month_to_month = models.BooleanField(_("Month-to-Month"), default=False)
    move_in_date = models.DateField(_("Move-in Date"), null=True, blank=True)
    move_out_date = models.DateField(_("Move-out Date"), null=True, blank=True)
    
    # Financial details
    security_deposit = models.DecimalField(_("Security Deposit"), max_digits=10, decimal_places=2, default=0)
    pet_deposit = models.DecimalField(_("Pet Deposit"), max_digits=10, decimal_places=2, default=0)
    cleaning_fee = models.DecimalField(
        _("Cleaning Fee (Overall Lease)"),
        max_digits=10, decimal_places=2, default=0,
        help_text=_("Overall fee for complete units; individual fees are in LeaseTenant for roommates")
    )
    
    # Additional details
    pets_allowed = models.BooleanField(_("Pets Allowed"), default=False)
    smoking_allowed = models.BooleanField(_("Smoking Allowed"), default=False)
    bills_included = models.JSONField(
        _("Bills Included"), 
        default=dict, 
        blank=True,
        help_text=_(
            "JSON format for utilities with provider names, tenant responsibility, and distribution. "
            "Example: {'electricity': {'included': false, 'provider': 'BC Hydro', "
            "'tenant_responsibility': {'type': 'percentage', 'value': 75, 'distribution': 'equal'}}}"
        )
    )
    special_terms = models.TextField(_("Special Terms"), blank=True)
    
    # Document handling
    document_file = models.FileField(_("Main Agreement Document"), upload_to="lease_documents/%Y/%m/", null=True, blank=True)
    
    # If lease was renewed/replaced
    previous_lease = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='renewal_leases',
        help_text=_("Link to the lease this one renewed/replaced")
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _("Lease")
        verbose_name_plural = _("Leases")
        ordering = ["-start_date", "-created_at"]
    
    def __str__(self):
        lease_id = self.lease_number or f"Draft-{self.id.hex[:6]}"
        if self.property:
            return f"{self.get_lease_type_display()} - {self.property.name} ({lease_id})"
        elif self.group:
            return f"{self.get_lease_type_display()} - Group: {self.group.name} ({lease_id})"
        return f"{self.get_lease_type_display()} ({lease_id})"
    
    def clean(self):
        super().clean()
        
        # Validate that either property OR group is set, but not both or neither
        if (self.property and self.group) or (not self.property and not self.group):
            raise ValidationError(_("A lease must be associated with either a property OR a property group, not both or neither."))
        
        # Validate lease type matches property category
        is_roommate_type = "ROOMMATE" in self.lease_type
        is_residential_type = "RESIDENTIAL" in self.lease_type
        
        if self.property:
            if is_roommate_type and self.property.property_category != Property.PropertyCategory.ROOM:
                raise ValidationError(_("Roommate agreement types can only be used with Room properties."))
            if is_residential_type and self.property.property_category != Property.PropertyCategory.COMPLETE_UNIT:
                raise ValidationError(_("Residential agreement types can only be used with Complete Unit properties."))
        
        if self.group and not is_roommate_type:
            raise ValidationError(_("Leases linked to a Property Group must be a Roommate agreement type."))
        
        # Ensure landlord consistency
        if self.property and self.property.landlord != self.landlord:
            raise ValidationError(_("The landlord must own the property associated with this lease."))
        if self.group and self.group.landlord != self.landlord:
            raise ValidationError(_("The landlord must own the property group associated with this lease."))
        
        # End date validation
        if self.is_month_to_month and self.end_date:
            raise ValidationError(_("Month-to-month leases should not have an end date."))
        if not self.is_month_to_month and not self.end_date:
            raise ValidationError(_("Fixed-term leases must have an end date."))
        
        # Start/End date logic
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError(_("End date cannot be before the start date."))
        if self.move_out_date and self.move_in_date and self.move_out_date < self.move_in_date:
            raise ValidationError(_("Move-out date cannot be before the move-in date."))
        if self.move_in_date and self.move_in_date < self.start_date:
            raise ValidationError(_("Move-in date cannot be before the lease start date."))
            
        # Validate bills_included structure if provided
        if self.bills_included:
            self._validate_bills_included()
    
    def _validate_bills_included(self):
        """Validate the structure and values in the bills_included field."""
        VALID_BILL_CATEGORIES = {
            'electricity', 'water', 'gas', 'internet', 'waste', 'heat', 'cable', 'sewer'
        }
        
        VALID_RESPONSIBILITY_TYPES = {'none', 'percentage', 'fixed', 'full'}
        VALID_DISTRIBUTION_TYPES = {'none', 'equal', 'weighted', 'custom'}
        
        for bill_key, bill_data in self.bills_included.items():
            if not isinstance(bill_data, dict):
                raise ValidationError(_(f"Bill {bill_key} must be an object"))
            
            # Check required fields
            required_fields = ['included', 'provider', 'category']
            for field in required_fields:
                if field not in bill_data:
                    raise ValidationError(_(f"Bill {bill_key} is missing required field: {field}"))
            
            # Check category is valid
            if bill_data['category'] not in VALID_BILL_CATEGORIES:
                raise ValidationError(_(
                    f"Invalid category '{bill_data['category']}' for bill {bill_key}. "
                    f"Valid categories are: {', '.join(VALID_BILL_CATEGORIES)}"
                ))
            
            # If not included in rent, must have tenant_responsibility
            if not bill_data.get('included', True):
                if 'tenant_responsibility' not in bill_data:
                    raise ValidationError(_(
                        f"Bill {bill_key} is not included in rent but missing tenant_responsibility details"
                    ))
                
                resp = bill_data['tenant_responsibility']
                if not isinstance(resp, dict):
                    raise ValidationError(_(f"tenant_responsibility for {bill_key} must be an object"))
                
                # Check responsibility fields
                if 'type' not in resp:
                    raise ValidationError(_(f"tenant_responsibility for {bill_key} missing 'type'"))
                
                if resp['type'] not in VALID_RESPONSIBILITY_TYPES:
                    raise ValidationError(_(
                        f"Invalid responsibility type '{resp['type']}' for {bill_key}. "
                        f"Valid types are: {', '.join(VALID_RESPONSIBILITY_TYPES)}"
                    ))
                
                if 'distribution' not in resp:
                    raise ValidationError(_(f"tenant_responsibility for {bill_key} missing 'distribution'"))
                
                if resp['distribution'] not in VALID_DISTRIBUTION_TYPES:
                    raise ValidationError(_(
                        f"Invalid distribution type '{resp['distribution']}' for {bill_key}. "
                        f"Valid types are: {', '.join(VALID_DISTRIBUTION_TYPES)}"
                    ))
                
                # Additional validation based on responsibility type
                if resp['type'] != 'none' and ('value' not in resp or not isinstance(resp['value'], (int, float))):
                    raise ValidationError(_(f"tenant_responsibility for {bill_key} requires a numeric 'value'"))
                
                # Validate percentage is between 0-100
                if resp['type'] == 'percentage' and (resp['value'] < 0 or resp['value'] > 100):
                    raise ValidationError(_(f"Percentage value for {bill_key} must be between 0 and 100"))
                
                # Validate custom distribution if specified
                if resp['distribution'] == 'custom':
                    if 'custom_splits' not in resp or not isinstance(resp['custom_splits'], dict):
                        raise ValidationError(_(
                            f"Custom distribution for {bill_key} requires 'custom_splits' object"
                        ))
                    
                    splits_total = sum(resp['custom_splits'].values())
                    if abs(splits_total - 100) > 0.01:  # Allow small floating point differences
                        raise ValidationError(_(
                            f"Custom splits for {bill_key} must add up to 100%, got {splits_total}%"
                        ))
    
    def save(self, *args, **kwargs):
        # Generate lease number if not provided and instance is being created
        if not self.lease_number:  # Remove the self.pk check
            timestamp = int(timezone.now().timestamp())
            random_suffix = uuid.uuid4().hex[:4].upper()
            prefix = "RMT" if "ROOMMATE" in self.lease_type else "RES"
            self.lease_number = f"{prefix}{timestamp % 1000000}-{random_suffix}"
        
        self.full_clean()  # Run validation before saving
        super().save(*args, **kwargs)
    
    def get_total_monthly_rent(self):
        """Calculates total monthly rent from associated LeaseTenant records."""
        total = self.lease_tenants.aggregate(total=Sum('rent_amount'))['total']
        return total or 0
    
    def get_current_tenant_count(self):
        """Gets the current number of tenants associated with this lease."""
        return self.lease_tenants.count()
    
    def get_max_occupancy(self):
        """Gets the maximum occupancy based on the linked property or group."""
        if self.property:
            if self.property.property_category == Property.PropertyCategory.COMPLETE_UNIT:
                return self.property.max_occupancy or 1  # Default to 1 if not set
            elif self.property.property_category == Property.PropertyCategory.ROOM:
                return 1  # A single room lease implies occupancy of 1 for that room
        elif self.group:
            # Max occupancy for a group lease is the number of rooms in that group
            return self.group.grouped_properties.filter(property_category=Property.PropertyCategory.ROOM).count()
        return 0
    
    def get_bills_summary(self):
        """
        Returns a human-readable summary of bills and tenant responsibilities.
        """
        if not self.bills_included:
            return "No bills information available"
        
        summaries = []
        for bill_type, details in self.bills_included.items():
            if not isinstance(details, dict):
                continue
                
            provider = details.get('provider', '')
            category = details.get('category', bill_type)
            display = f"{provider}"
            
            if details.get('included', False):
                summaries.append(f"{display} - Included in rent")
            else:
                resp = details.get('tenant_responsibility', {})
                resp_type = resp.get('type')
                
                if resp_type == 'full':
                    summaries.append(f"{display} - Tenant pays 100%")
                elif resp_type == 'percentage':
                    value = resp.get('value', 0)
                    summaries.append(f"{display} - Tenant pays {value}%")
                elif resp_type == 'fixed':
                    value = resp.get('value', 0)
                    summaries.append(f"{display} - Tenant pays ${value}/month")
                else:
                    summaries.append(f"{display} - {details.get('notes', '')}")
        
        if not summaries:
            return "No bills information available"
        
        return "; ".join(summaries)
    
    def calculate_tenant_bill_share(self, tenant_id, bill_type, bill_amount):
        """
        Calculates a specific tenant's share of a given bill.
        
        Args:
            tenant_id: The UUID of the tenant
            bill_type: The type of bill (e.g., 'electricity')
            bill_amount: The total bill amount
            
        Returns:
            Decimal: The tenant's share of the bill
        """
        if not self.bills_included or bill_type not in self.bills_included:
            return Decimal('0.00')
            
        bill_details = self.bills_included[bill_type]
        
        # If bill is included in rent, tenant pays nothing extra
        if bill_details.get('included', True):
            return Decimal('0.00')
            
        resp = bill_details.get('tenant_responsibility', {})
        resp_type = resp.get('type')
        
        # If no tenant responsibility defined, return 0
        if not resp_type or resp_type == 'none':
            return Decimal('0.00')
            
        # Get all tenant IDs in the lease for distribution calculations
        tenant_ids = [str(lt.tenant.id) for lt in self.lease_tenants.all()]
        tenant_count = len(tenant_ids)
        
        if tenant_count == 0:
            return Decimal('0.00')
            
        # Calculate tenant's share based on responsibility type and distribution
        tenant_share = Decimal('0.00')
        
        if resp_type == 'full':
            # Tenant pays 100% of the bill (subject to distribution)
            tenant_portion = Decimal(bill_amount)
        elif resp_type == 'percentage':
            # Tenant pays a percentage of the bill
            percentage = Decimal(resp.get('value', 0)) / Decimal('100.0')
            tenant_portion = bill_amount * percentage
        elif resp_type == 'fixed':
            # Tenant pays a fixed amount (not affected by actual bill)
            # Return the fixed amount divided by the distribution
            fixed_amount = Decimal(resp.get('value', 0))
            distribution = resp.get('distribution')
            
            if distribution == 'custom':
                custom_splits = resp.get('custom_splits', {})
                tenant_percentage = Decimal(custom_splits.get(str(tenant_id), 0)) / Decimal('100.0')
                return fixed_amount * tenant_percentage
            elif distribution == 'equal':
                return fixed_amount / Decimal(tenant_count)
            elif distribution == 'weighted':
                # For fixed amounts, weighted distribution may not make sense
                # Fallback to equal distribution
                return fixed_amount / Decimal(tenant_count)
            else:
                return fixed_amount
        else:
            return Decimal('0.00')
        
        # Apply distribution to the tenant portion
        distribution = resp.get('distribution')
        
        if distribution == 'equal':
            # Split equally among all tenants
            tenant_share = tenant_portion / Decimal(tenant_count)
        elif distribution == 'custom':
            # Use custom split percentages
            custom_splits = resp.get('custom_splits', {})
            tenant_percentage = Decimal(custom_splits.get(str(tenant_id), 0)) / Decimal('100.0')
            tenant_share = tenant_portion * tenant_percentage
        elif distribution == 'weighted':
            # Weight by rent amount
            total_rent = self.get_total_monthly_rent()
            if total_rent > 0:
                try:
                    tenant_lease = self.lease_tenants.get(tenant__id=tenant_id)
                    tenant_rent = tenant_lease.rent_amount
                    tenant_share = tenant_portion * (tenant_rent / total_rent)
                except:
                    # Fallback to equal distribution if tenant not found
                    tenant_share = tenant_portion / Decimal(tenant_count)
            else:
                # Fallback to equal distribution if total rent is 0
                tenant_share = tenant_portion / Decimal(tenant_count)
        else:
            # Default to equal distribution
            tenant_share = tenant_portion / Decimal(tenant_count)
        
        return tenant_share.quantize(Decimal('0.01'))  # Round to 2 decimal places


class LeaseTenant(models.Model):
    """Links a TenantProfile to a Lease, specifying their individual terms."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name="lease_tenants")
    tenant = models.ForeignKey(TenantProfile, on_delete=models.PROTECT, related_name="tenant_leases")
    rent_amount = models.DecimalField(_("Individual Monthly Rent"), max_digits=10, decimal_places=2)
    
    # For roommate agreements, link to specific room if applicable
    room = models.ForeignKey(
        Property,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="room_tenants",
        limit_choices_to={'property_category': Property.PropertyCategory.ROOM},
        help_text=_("Specific room assignment within a group lease (Roommate Agreements only)")
    )
    
    # Individual cleaning fee for roommate agreements
    cleaning_fee = models.DecimalField(
        _("Individual Cleaning Fee"), max_digits=10, decimal_places=2, default=0,
        help_text=_("Cleaning fee charged specifically to this tenant (for roommate leases)")
    )
    cleaning_fee_paid = models.BooleanField(_("Cleaning Fee Paid"), default=False)
    is_primary_tenant = models.BooleanField(
        _("Primary Tenant"), default=False,
        help_text=_("Is this the primary contact for communications regarding the lease?")
    )
    has_signed = models.BooleanField(_("Has Signed Agreement"), default=False)
    signed_date = models.DateTimeField(_("Date Signed"), null=True, blank=True)
    
    # Individual tenant dates (if they differ from main lease, e.g., late joiner)
    individual_start_date = models.DateField(
        _("Individual Start Date"), null=True, blank=True,
        help_text=_("Tenant's specific start date if different from lease.start_date")
    )
    individual_end_date = models.DateField(
        _("Individual End Date"), null=True, blank=True,
        help_text=_("Tenant's specific end date if different from lease.end_date")
    )
    
    tenant_notes = models.TextField(_("Notes specific to this tenant"), blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _("Lease Tenant")
        verbose_name_plural = _("Lease Tenants")
        unique_together = [('lease', 'tenant')]
        ordering = ['lease', 'tenant__user__name']
    
    def __str__(self):
        return f"{self.tenant.user.name} on Lease {self.lease.lease_number or self.lease.id.hex[:6]}"
    
    def clean(self):
        super().clean()
        # Ensure room is only set for roommate-type leases linked to a group
        if self.room and not ("ROOMMATE" in self.lease.lease_type and self.lease.group):
            raise ValidationError(_("A specific room can only be assigned if the lease is a Roommate type linked to a Property Group."))
        
        # Ensure assigned room belongs to the correct group
        if self.room and self.lease.group and self.room.group != self.lease.group:
            raise ValidationError(_(f"The assigned room '{self.room.name}' does not belong to the lease's group '{self.lease.group.name}'."))
        
        # Ensure individual cleaning fee is only non-zero for roommate types
        if self.cleaning_fee != 0 and not ("ROOMMATE" in self.lease.lease_type):
            raise ValidationError(_("Individual cleaning fees should only be set for tenants on Roommate agreement types."))
        
        # Ensure individual dates are within lease dates if set
        if self.individual_start_date and self.individual_start_date < self.lease.start_date:
            raise ValidationError(_("Individual start date cannot be before the main lease start date."))
        if self.individual_end_date and self.lease.end_date and self.individual_end_date > self.lease.end_date:
            raise ValidationError(_("Individual end date cannot be after the main lease end date."))
        if self.individual_start_date and self.individual_end_date and self.individual_end_date < self.individual_start_date:
            raise ValidationError(_("Individual end date cannot be before the individual start date."))
    
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class LeaseDocument(models.Model):
    """Stores additional documents related to a specific lease."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name="additional_documents")
    title = models.CharField(_("Document Title"), max_length=255)
    document = models.FileField(_("Document File"), upload_to="lease_documents/%Y/%m/additional/")
    description = models.TextField(_("Description"), blank=True)
    is_signed = models.BooleanField(_("Is Signed"), default=False, help_text=_("Indicates if this specific document requires/has signatures"))
    uploaded_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = _("Lease Document")
        verbose_name_plural = _("Lease Documents")
        ordering = ["-uploaded_at"]
    
    def __str__(self):
        return f"{self.title} for Lease {self.lease.lease_number or self.lease.id.hex[:6]}"


class Payment(models.Model):
    class PaymentType(models.TextChoices):
        RENT = "RENT", _("Rent Payment")
        SECURITY_DEPOSIT = "SECURITY_DEPOSIT", _("Security Deposit")
        PET_DEPOSIT = "PET_DEPOSIT", _("Pet Deposit")
        CLEANING_FEE = "CLEANING_FEE", _("Cleaning Fee")
        LATE_FEE = "LATE_FEE", _("Late Fee")
        UTILITY = "UTILITY", _("Utility Payment")
        MAINTENANCE = "MAINTENANCE", _("Maintenance Fee/Chargeback")
        OTHER = "OTHER", _("Other")
    
    class PaymentStatus(models.TextChoices):
        SCHEDULED = "SCHEDULED", _("Scheduled")
        PENDING = "PENDING", _("Pending")
        PROCESSING = "PROCESSING", _("Processing")
        COMPLETED = "COMPLETED", _("Completed")
        PARTIALLY_PAID = "PARTIALLY_PAID", _("Partially Paid")
        OVERDUE = "OVERDUE", _("Overdue")
        FAILED = "FAILED", _("Failed")
        REFUNDED = "REFUNDED", _("Refunded")
        CANCELLED = "CANCELLED", _("Cancelled")
    
    class PaymentMethod(models.TextChoices):
        CASH = "CASH", _("Cash")
        CHEQUE = "CHEQUE", _("Cheque")
        ETRANSFER = "ETRANSFER", _("E-Transfer")
        BANK_TRANSFER = "BANK_TRANSFER", _("Bank Transfer")
        CREDIT_CARD = "CREDIT_CARD", _("Credit Card")
        DEBIT_CARD = "DEBIT_CARD", _("Debit Card")
        PAYPAL = "PAYPAL", _("PayPal")
        STRIPE = "STRIPE", _("Stripe")
        OTHER = "OTHER", _("Other")
        NA = "NA", _("N/A")
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="payments")
    tenant = models.ForeignKey(TenantProfile, on_delete=models.PROTECT, related_name="payments")
    payment_type = models.CharField(_("Payment Type"), max_length=20, choices=PaymentType.choices)
    amount_due = models.DecimalField(_("Amount Due"), max_digits=10, decimal_places=2)
    amount_paid = models.DecimalField(_("Amount Paid"), max_digits=10, decimal_places=2, null=True, blank=True)
    due_date = models.DateField(_("Due Date"))
    payment_date = models.DateField(_("Payment Date"), null=True, blank=True, help_text=_("Date payment was completed"))
    status = models.CharField(_("Status"), max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.SCHEDULED)
    payment_method = models.CharField(
        _("Payment Method"), max_length=20, choices=PaymentMethod.choices,
        null=True, blank=True, default=PaymentMethod.NA
    )
    reference_number = models.CharField(
        _("Reference/Transaction ID"), max_length=100, blank=True,
        help_text=_("Optional reference for tracking (e.g., cheque number, transaction ID)")
    )
    notes = models.TextField(_("Notes"), blank=True)
    receipt_file = models.FileField(_("Receipt File"), upload_to="payment_receipts/%Y/%m/", null=True, blank=True)
    
    # For utility payments, specify which utility this payment is for
    utility_type = models.CharField(_("Utility Type"), max_length=50, blank=True, help_text=_("If this is a utility payment, specify which utility (e.g., electricity, water)"))
    utility_provider = models.CharField(_("Utility Provider"), max_length=100, blank=True, help_text=_("Provider name for utility payments"))
    utility_period_start = models.DateField(_("Utility Period Start"), null=True, blank=True)
    utility_period_end = models.DateField(_("Utility Period End"), null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _("Payment")
        verbose_name_plural = _("Payments")
        ordering = ["due_date", "created_at"]
    
    def __str__(self):
        return f"{self.get_payment_type_display()} of {self.amount_due} due {self.due_date} for {self.tenant.user.name} (Lease {self.lease.lease_number or self.lease.id.hex[:6]})"
    
    def clean(self):
        super().clean()
        if self.amount_paid is not None and self.amount_paid < 0:
            raise ValidationError(_("Amount paid cannot be negative."))
        if self.amount_due < 0:
            raise ValidationError(_("Amount due cannot be negative."))
        
        # Validate utility-specific fields
        if self.payment_type == self.PaymentType.UTILITY:
            if not self.utility_type:
                raise ValidationError(_("Utility type must be specified for utility payments."))
            if not self.utility_provider:
                raise ValidationError(_("Utility provider must be specified for utility payments."))
    
    def save(self, *args, **kwargs):
        # Update status based on payment details before saving
        today = timezone.now().date()
        if self.amount_paid is not None:
            if self.status not in [Payment.PaymentStatus.REFUNDED, Payment.PaymentStatus.CANCELLED]:
                if self.amount_paid >= self.amount_due:
                    self.status = Payment.PaymentStatus.COMPLETED
                    if not self.payment_date:
                        self.payment_date = today
                elif self.amount_paid > 0:
                    self.status = Payment.PaymentStatus.PARTIALLY_PAID
                    if not self.payment_date:
                        self.payment_date = today
        elif self.status == Payment.PaymentStatus.SCHEDULED and self.due_date <= today:
            self.status = Payment.PaymentStatus.PENDING
        elif self.status == Payment.PaymentStatus.PENDING and self.due_date < today:
            self.status = Payment.PaymentStatus.OVERDUE
            
        self.full_clean()
        super().save(*args, **kwargs)


class PaymentReminder(models.Model):
    """Stores scheduled reminders for upcoming or overdue payments."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="reminders")
    reminder_date = models.DateField(_("Reminder Date"), help_text=_("Date the reminder should be sent"))
    message_template = models.TextField(_("Message Template"), blank=True, help_text=_("Optional custom message, otherwise default used"))
    is_sent = models.BooleanField(_("Is Sent"), default=False)
    sent_date = models.DateTimeField(_("Date Sent"), null=True, blank=True)
    send_method = models.CharField(_("Send Method"), max_length=10, default="EMAIL", choices=[("EMAIL", "Email"), ("SMS", "SMS"), ("APP", "In-App")])
    error_message = models.TextField(_("Error Message"), blank=True, help_text=_("Records any error during sending"))
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = _("Payment Reminder")
        verbose_name_plural = _("Payment Reminders")
        ordering = ["reminder_date"]
    
    def __str__(self):
        status = "Sent" if self.is_sent else "Pending"
        return f"{status} reminder for Payment {self.payment_id} on {self.reminder_date}"