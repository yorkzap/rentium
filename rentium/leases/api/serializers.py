from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from rentium.leases.models import Lease
from rentium.leases.models import LeaseDocument
from rentium.leases.models import LeaseLandlordSignatory
from rentium.leases.models import LeaseTenant
from rentium.leases.models import Payment
from rentium.leases.models import PaymentReminder
from rentium.leases.models import RentAdjustment
from rentium.leases.services import compute_rent_split
from rentium.properties.models import Property
from rentium.properties.models import PropertyGroup
from rentium.users.models import TenantProfile

User = get_user_model()


class TenantBasicSerializer(serializers.ModelSerializer):
    """Basic serializer for tenant information"""

    name = serializers.CharField(source="user.name", read_only=True)
    email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = TenantProfile
        fields = ["id", "name", "email"]


class RentAdjustmentSerializer(serializers.ModelSerializer):
    adjusted_preview = serializers.SerializerMethodField()

    class Meta:
        model = RentAdjustment
        fields = [
            "id",
            "lease_tenant",
            "adjustment_type",
            "calculation_method",
            "amount",
            "nights_charged",
            "nights_in_period",
            "reason",
            "effective_date",
            "end_date",
            "is_recurring",
            "created_by",
            "created_at",
            "updated_at",
            "adjusted_preview",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def get_adjusted_preview(self, obj):
        """Shows what the resulting rent would be, given the parent LeaseTenant's base rent."""
        try:
            return obj.get_adjusted_amount(obj.lease_tenant.rent_amount)
        except Exception:
            return None

    def validate(self, data):
        lease_tenant = data.get("lease_tenant") or getattr(
            self.instance, "lease_tenant", None
        )
        try:
            temp_instance = self.instance or RentAdjustment(lease_tenant=lease_tenant)
            for key, value in data.items():
                setattr(temp_instance, key, value)
            temp_instance.clean()
        except DjangoValidationError as e:
            raise serializers.ValidationError(serializers.as_serializer_error(e))
        return data


class LeaseLandlordSignatorySerializer(serializers.ModelSerializer):
    """A co-landlord who is a signing party on the lease (read-only display)."""

    display_name = serializers.CharField(read_only=True)
    is_linked = serializers.SerializerMethodField()

    class Meta:
        model = LeaseLandlordSignatory
        fields = [
            "id",
            "display_name",
            "name",
            "email",
            "phone",
            "has_signed",
            "signed_date",
            "is_linked",
        ]

    def get_is_linked(self, obj):
        return obj.member_id is not None


class LeaseTenantSerializer(serializers.ModelSerializer):
    """
    Single source of truth for LeaseTenant serialization. Supports two ways
    of attaching a tenant on create:
      1. `tenant_id` — directly link an existing TenantProfile you already
         know the ID of (e.g. picked from `available_tenants`).
      2. `invited_email` (+ optional `invited_name` / `invited_phone`) —
         invite someone by email who may not have an account yet. The
         invited_name is the full legal name the landlord entered; it
         pre-fills the lease form (RTB-1 parties/signature blocks) until
         the tenant links an account, at which point the account's own
         name takes over.
    Exactly one of `tenant_id` / `invited_email` must be provided on create.
    """

    tenant = serializers.SerializerMethodField()
    tenant_name = serializers.SerializerMethodField()
    tenant_email = serializers.SerializerMethodField()
    room_name = serializers.CharField(
        source="room.name", read_only=True, allow_null=True
    )
    invite_status = serializers.SerializerMethodField()
    invite_lifecycle = serializers.SerializerMethodField()
    invite_url = serializers.SerializerMethodField()
    effective_rent = serializers.SerializerMethodField()
    rent_adjustments = RentAdjustmentSerializer(many=True, read_only=True)
    # Writable linking fields
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=TenantProfile.objects.all(),
        source="tenant",
        write_only=True,
        required=False,
        allow_null=True,
    )
    room_id = serializers.PrimaryKeyRelatedField(
        queryset=Property.objects.filter(
            property_category=Property.PropertyCategory.ROOM
        ),
        source="room",
        write_only=True,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = LeaseTenant
        fields = [
            "id",
            "lease",
            "tenant_id",
            "tenant",
            "tenant_name",
            "tenant_email",
            "rent_amount",
            "effective_rent",
            "room_id",
            "room_name",
            "cleaning_fee",
            "cleaning_fee_paid",
            "is_primary_tenant",
            "has_signed",
            "signed_date",
            "declined",
            "declined_at",
            "decline_reason",
            "individual_start_date",
            "individual_end_date",
            "invited_email",
            "invited_name",
            "invited_phone",
            "invite_status",
            "invite_lifecycle",
            "invite_url",
            "invite_sent_at",
            "invite_accepted_at",
            "tenant_notes",
            "rent_adjustments",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "tenant_name",
            "tenant_email",
            "room_name",
            "has_signed",
            "signed_date",
            "declined",
            "declined_at",
            "cleaning_fee_paid",
            "invite_status",
            "invite_lifecycle",
            "invite_sent_at",
            "invite_accepted_at",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "lease": {"required": False, "write_only": True},
            "rent_amount": {
                "required": False,
                "help_text": (
                    "Leave blank to auto-fill: the full total_rent if this is the "
                    "only tenant on the lease, otherwise an equal split of "
                    "whatever's still unallocated."
                ),
            },
        }

    def get_tenant(self, obj):
        return obj.tenant_id

    def get_tenant_name(self, obj):
        return obj.tenant.user.name if obj.tenant else None

    def get_tenant_email(self, obj):
        return obj.tenant.user.email if obj.tenant else None

    def get_invite_status(self, obj):
        if obj.declined:
            return "DECLINED"
        if obj.tenant:
            return "LINKED"
        if obj.invite_accepted_at:
            return "ACCEPTED"
        if obj.invite_sent_at:
            return "PENDING"
        return "NOT_SENT"

    def get_invite_lifecycle(self, obj):
        from rentium.leases.services import invite_lifecycle

        return invite_lifecycle(obj)

    def get_invite_url(self, obj):
        """
        Only the owning landlord can retrieve this — it's a live credential
        (whoever has it can create the account for this slot). Returns None
        for anyone else, and None once the slot is already linked (nothing
        left to retrieve — see LeaseTenant.get_invite_url()).
        """
        request = self.context.get("request")
        if not request or not hasattr(request.user, "landlord_profile"):
            return None
        if request.user.landlord_profile != obj.lease.landlord:
            return None
        frontend_base = getattr(settings, "FRONTEND_URL", "http://localhost:3000")
        return obj.get_invite_url(frontend_base)

    def get_effective_rent(self, obj):
        """Base rent net of any currently-active adjustments (for display, not billing)."""
        today = timezone.now().date()
        rent = obj.rent_amount
        active_adjustments = obj.rent_adjustments.filter(
            effective_date__lte=today
        ).filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        for adj in active_adjustments:
            rent = adj.get_adjusted_amount(rent)
        return rent

    def validate(self, data):
        lease = self.context.get("lease") or data.get("lease")
        if not self.instance and not lease:
            raise serializers.ValidationError("Lease context is required.")
        tenant = data.get("tenant")
        invited_email = data.get("invited_email")
        if not self.instance:
            if not tenant and not invited_email:
                raise serializers.ValidationError(
                    "Provide either tenant_id (existing tenant) or invited_email (new invite)."
                )
            if tenant and invited_email:
                raise serializers.ValidationError(
                    "Provide only one of tenant_id or invited_email, not both."
                )
            if (
                tenant
                and LeaseTenant.objects.filter(lease=lease, tenant=tenant).exists()
            ):
                raise serializers.ValidationError(
                    {
                        "tenant_id": f"Tenant {tenant.user.name} is already associated with this lease."
                    }
                )
            if (
                invited_email
                and LeaseTenant.objects.filter(
                    lease=lease, invited_email__iexact=invited_email
                ).exists()
            ):
                raise serializers.ValidationError(
                    {
                        "invited_email": "This email has already been invited to this lease."
                    }
                )
        # Validate using model's clean method
        try:
            temp_instance = self.instance or LeaseTenant(lease=lease)
            for key, value in data.items():
                setattr(temp_instance, key, value)
            temp_instance.clean()
        except DjangoValidationError as e:
            raise serializers.ValidationError(serializers.as_serializer_error(e))
        # --- Once a tenant has signed, their rent share is locked ---
        # Applies on update only. A signature is agreement to a specific
        # number — quietly changing it afterward (even while the lease is
        # still PENDING_SIGNATURES and technically not "locked" yet) would
        # mean they're bound to a figure they never actually agreed to.
        # Anything else about their row (notes, dates, etc.) can still be
        # edited; only rent_amount is frozen once signed.
        if self.instance and self.instance.has_signed and "rent_amount" in data:
            if Decimal(str(data["rent_amount"])) != Decimal(self.instance.rent_amount):
                raise serializers.ValidationError(
                    {
                        "rent_amount": (
                            "This tenant has already signed at their current rent amount "
                            "and it can no longer be changed here. Adjust it via a "
                            "RentAdjustment (for an ongoing discount/increase going "
                            "forward) or through Django admin if the original figure "
                            "was genuinely wrong."
                        )
                    }
                )
        # --- Invited name is frozen once the slot signs or links ---
        # A linked account's own name is authoritative (invited_name is just
        # a fallback then), and a signed slot's identity shouldn't shift.
        if self.instance and "invited_name" in data:
            changed = (data["invited_name"] or "") != (self.instance.invited_name or "")
            if changed and (self.instance.has_signed or self.instance.tenant_id):
                raise serializers.ValidationError(
                    {
                        "invited_name": (
                            "The name can't be edited after the tenant has signed or "
                            "linked their account — their account name is used from "
                            "that point."
                        )
                    }
                )
        # --- Rent auto-fill / over-allocation guard ---
        # Only applies on create, and only when the landlord is using the
        # total_rent feature at all (total_rent > 0). Leases with
        # total_rent left at its default of 0 skip this entirely — that's
        # the "not using auto-split" escape hatch for anyone setting rent
        # amounts the old way.
        #
        # Uses the same compute_rent_split() the `preview-split` endpoint
        # uses, rather than a second inline implementation of the same
        # rule — this is the create-a-single-new-tenant special case of
        # that same algorithm: all existing tenants are "fixed" (already
        # saved, so effectively touched), and this one new row is the only
        # editable one, UNLESS an explicit amount was provided for it too
        # (in which case it's fixed too, and over-allocation is checked
        # directly rather than silently overwritten).
        if not self.instance and lease.total_rent and lease.total_rent > 0:
            existing_rows = [
                {
                    "id": str(lt.id),
                    "rent_amount": lt.rent_amount,
                    "touched": True,
                    "has_signed": lt.has_signed,
                }
                for lt in lease.lease_tenants.all()
            ]
            provided_amount = data.get("rent_amount")
            new_row = {
                "id": None,
                "rent_amount": Decimal(str(provided_amount))
                if provided_amount not in (None, "")
                else None,
                "touched": provided_amount not in (None, ""),
                "has_signed": False,
            }
            if new_row["touched"]:
                # An explicit amount was given — validate it doesn't
                # over-allocate rather than silently letting
                # compute_rent_split treat it as authoritative.
                already_allocated = sum(
                    (r["rent_amount"] for r in existing_rows), Decimal("0.00")
                )
                unallocated = Decimal(lease.total_rent) - already_allocated
                if new_row["rent_amount"] > unallocated + Decimal("0.01"):
                    raise serializers.ValidationError(
                        {
                            "rent_amount": (
                                f"This would over-allocate the lease's total rent. "
                                f"Only ${unallocated} is still unassigned."
                            )
                        }
                    )
            else:
                computed = compute_rent_split(
                    existing_rows + [new_row], lease.total_rent
                )
                data["rent_amount"] = computed[-1]["rent_amount"]
        return data

    def create(self, validated_data):
        invited_email = validated_data.get("invited_email")
        # If inviting by email and an account already exists for it, skip the
        # pending-invite state and link immediately.
        if invited_email and not validated_data.get("tenant"):
            existing_user = User.objects.filter(email__iexact=invited_email).first()
            if existing_user and hasattr(existing_user, "tenant_profile"):
                validated_data["tenant"] = existing_user.tenant_profile
                validated_data["invite_accepted_at"] = timezone.now()
        if invited_email:
            validated_data["invite_sent_at"] = timezone.now()
        return super().create(validated_data)


class LeaseDocumentSerializer(serializers.ModelSerializer):
    lease_number = serializers.CharField(source="lease.lease_number", read_only=True)

    class Meta:
        model = LeaseDocument
        fields = [
            "id",
            "lease",
            "lease_number",
            "title",
            "document",
            "description",
            "is_signed",
            "uploaded_at",
        ]
        read_only_fields = ["id", "lease_number", "uploaded_at"]
        extra_kwargs = {"lease": {"required": False, "write_only": True}}


class PaymentReminderSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentReminder
        fields = [
            "id",
            "payment",
            "reminder_date",
            "message_template",
            "is_sent",
            "sent_date",
            "send_method",
            "error_message",
            "created_at",
        ]
        read_only_fields = ["id", "is_sent", "sent_date", "error_message", "created_at"]
        extra_kwargs = {"payment": {"required": False}}


class PaymentSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source="tenant.user.name", read_only=True)
    payment_type_display = serializers.CharField(
        source="get_payment_type_display", read_only=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    payment_method_display = serializers.CharField(
        source="get_payment_method_display", read_only=True, allow_null=True
    )
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=TenantProfile.objects.all(), source="tenant", write_only=True
    )
    reminders = PaymentReminderSerializer(many=True, read_only=True)

    class Meta:
        model = Payment
        fields = [
            "id",
            "lease",
            "tenant_id",
            "tenant_name",
            "payment_type",
            "payment_type_display",
            "amount_due",
            "amount_paid",
            "due_date",
            "payment_date",
            "status",
            "status_display",
            "payment_method",
            "payment_method_display",
            "reference_number",
            "rent_adjustment",
            "notes",
            "receipt_file",
            "utility_type",
            "utility_provider",
            "utility_period_start",
            "utility_period_end",
            "reminders",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "tenant_name",
            "payment_type_display",
            "status_display",
            "payment_method_display",
            "reminders",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "lease": {"required": False, "write_only": True},
            "amount_paid": {"allow_null": True},
            "payment_date": {"allow_null": True},
            "payment_method": {"allow_null": True},
            "utility_period_start": {"allow_null": True},
            "utility_period_end": {"allow_null": True},
            "rent_adjustment": {"required": False, "allow_null": True},
        }

    def validate(self, data):
        if data.get("amount_paid", 0) is not None and data.get("amount_paid", 0) < 0:
            raise serializers.ValidationError(
                {"amount_paid": "Amount paid cannot be negative."}
            )
        if data.get("amount_due", 0) < 0:
            raise serializers.ValidationError(
                {"amount_due": "Amount due cannot be negative."}
            )
        lease = self.context.get("lease") or data.get("lease")
        tenant = data.get("tenant")
        if lease and tenant and not lease.lease_tenants.filter(tenant=tenant).exists():
            raise serializers.ValidationError(
                {
                    "tenant_id": f"Tenant {tenant.user.name} is not associated with lease {lease.lease_number}."
                }
            )
        payment_type = data.get("payment_type")
        if payment_type == Payment.PaymentType.UTILITY:
            if not data.get("utility_type"):
                raise serializers.ValidationError(
                    {"utility_type": "Utility type is required for utility payments."}
                )
            if not data.get("utility_provider"):
                raise serializers.ValidationError(
                    {
                        "utility_provider": "Utility provider is required for utility payments."
                    }
                )
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

    lease_type_display = serializers.CharField(
        source="get_lease_type_display", read_only=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    property_name = serializers.CharField(
        source="property.name", read_only=True, allow_null=True
    )
    property_address = serializers.CharField(
        source="property.address", read_only=True, allow_null=True
    )
    group_name = serializers.CharField(
        source="group.name", read_only=True, allow_null=True
    )
    tenant_count = serializers.IntegerField(
        source="get_current_tenant_count", read_only=True
    )
    total_rent = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True, source="get_total_monthly_rent"
    )
    landlord_name = serializers.CharField(source="landlord.user.name", read_only=True)
    primary_tenant_name = serializers.SerializerMethodField()
    tenant_names = serializers.SerializerMethodField()

    class Meta:
        model = Lease
        fields = [
            "id",
            "lease_number",
            "lease_type",
            "lease_type_display",
            "status",
            "status_display",
            "property_name",
            "property_address",
            "group_name",
            "landlord_name",
            "primary_tenant_name",
            "tenant_names",
            "start_date",
            "end_date",
            "is_month_to_month",
            "tenant_count",
            "total_rent",
            "created_at",
        ]

    def get_tenant_names(self, obj):
        """All tenant display names on the lease (account name -> invited
        name -> invited email), primary first. Lets the leases-list page
        show WHO is on a lease when the tenant-count is clicked, without a
        detail fetch per row."""
        names = []
        for lt in obj.lease_tenants.select_related("tenant__user").order_by(
            "-is_primary_tenant", "invited_email"
        ):
            if lt.tenant:
                names.append(lt.tenant.user.name)
            elif lt.invited_name:
                names.append(lt.invited_name)
            elif lt.invited_email:
                names.append(lt.invited_email)
        return names

    def get_primary_tenant_name(self, obj):
        """
        Name of the lease's primary tenant, falling back to the first tenant
        slot if none is flagged primary. Fallback chain per slot: linked
        account name -> landlord-entered invited_name -> invited email.
        Used by the frontend to build the lease-selector label
        ("McKenzie Room A · Raja S. · Jan 1 – Month-to-month") — a bare
        lease number alone isn't enough to pick the right lease from a
        dropdown.
        NOTE: for list views, add
        Prefetch/prefetch_related("lease_tenants__tenant__user") to the
        LeaseViewSet list queryset if this ever shows up in query counts;
        with a handful of leases it's negligible.
        """
        lt = (
            obj.lease_tenants.filter(is_primary_tenant=True)
            .select_related("tenant__user")
            .first()
            or obj.lease_tenants.select_related("tenant__user").first()
        )
        if not lt:
            return None
        if lt.tenant:
            return lt.tenant.user.name
        return lt.invited_name or lt.invited_email or None


class LeaseSerializer(serializers.ModelSerializer):
    lease_type_display = serializers.CharField(
        source="get_lease_type_display", read_only=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    landlord_name = serializers.CharField(source="landlord.user.name", read_only=True)
    property_name = serializers.CharField(
        source="property.name", read_only=True, allow_null=True
    )
    property_address = serializers.CharField(
        source="property.address", read_only=True, allow_null=True
    )
    group_name = serializers.CharField(
        source="group.name", read_only=True, allow_null=True
    )
    bills_summary = serializers.CharField(source="get_bills_summary", read_only=True)
    common_space_clause_text = serializers.CharField(
        source="get_common_space_clause_text", read_only=True
    )
    effective_landlord_contact = serializers.SerializerMethodField()
    # Where tenants should send e-transfers for this lease — writable
    # etransfer_email + resolved effective value (with fallbacks) so the
    # tenant dashboard's "Make a Payment" section can always show a target.
    effective_etransfer_email = serializers.SerializerMethodField()
    is_locked = serializers.SerializerMethodField()
    # Raw FK ids for read (write goes through property_id/group_id below).
    # The appointments UI needs the property id to schedule visits.
    property = serializers.PrimaryKeyRelatedField(read_only=True)
    group = serializers.PrimaryKeyRelatedField(read_only=True)
    # Nested resources — now correctly using the single consolidated LeaseTenantSerializer
    lease_tenants = LeaseTenantSerializer(many=True, read_only=True)
    landlord_signatories = LeaseLandlordSignatorySerializer(many=True, read_only=True)
    additional_documents = LeaseDocumentSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    total_monthly_rent = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True, source="get_total_monthly_rent"
    )
    unallocated_rent = serializers.SerializerMethodField()
    current_tenant_count = serializers.IntegerField(
        read_only=True, source="get_current_tenant_count"
    )
    max_occupancy = serializers.IntegerField(read_only=True, source="get_max_occupancy")
    property_id = serializers.PrimaryKeyRelatedField(
        queryset=Property.objects.all(),
        source="property",
        write_only=True,
        required=False,
        allow_null=True,
    )
    group_id = serializers.PrimaryKeyRelatedField(
        queryset=PropertyGroup.objects.all(),
        source="group",
        write_only=True,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Lease
        fields = [
            "id",
            "lease_type",
            "lease_type_display",
            "property_id",
            "property",
            "property_name",
            "property_address",
            "group_id",
            "group",
            "group_name",
            "landlord",
            "landlord_name",
            "lease_number",
            "status",
            "status_display",
            "is_locked",
            "start_date",
            "end_date",
            "is_month_to_month",
            "move_in_date",
            "move_out_date",
            "security_deposit",
            "pet_deposit",
            "cleaning_fee",
            "pets_allowed",
            "smoking_allowed",
            "bills_included",
            "bills_summary",
            "special_terms",
            "co_hosts",
            "common_space_shared_with",
            "common_space_clause_text",
            "landlord_service_address",
            "landlord_daytime_phone",
            "landlord_other_phone",
            "landlord_fax",
            "landlord_service_email",
            "etransfer_email",
            "effective_etransfer_email",
            "effective_landlord_contact",
            "fixed_term_end_reason",
            "fixed_term_end_regulation_section",
            "custom_tenant_notice_months",
            "landlord_signed",
            "landlord_signed_date",
            "document_file",
            "previous_lease",
            "created_at",
            "updated_at",
            "lease_tenants",
            "landlord_signatories",
            "additional_documents",
            "payments",
            "total_rent",
            "total_monthly_rent",
            "unallocated_rent",
            "current_tenant_count",
            "max_occupancy",
        ]
        read_only_fields = [
            "id",
            "landlord",
            "landlord_name",
            "lease_number",
            "lease_type_display",
            "status_display",
            "is_locked",
            "property_name",
            "property_address",
            "group_name",
            "bills_summary",
            "co_hosts",
            "common_space_clause_text",
            "effective_landlord_contact",
            "effective_etransfer_email",
            "landlord_signed",
            "landlord_signed_date",
            "created_at",
            "updated_at",
            "lease_tenants",
            "landlord_signatories",
            "additional_documents",
            "payments",
            "total_monthly_rent",
            "unallocated_rent",
            "current_tenant_count",
            "max_occupancy",
        ]
        extra_kwargs = {
            "landlord": {"read_only": True},
            "previous_lease": {"allow_null": True},
        }

    def get_effective_landlord_contact(self, obj):
        return obj.get_effective_landlord_contact()

    def get_effective_etransfer_email(self, obj):
        return obj.get_effective_etransfer_email()

    def get_is_locked(self, obj):
        return obj.is_locked()

    def get_unallocated_rent(self, obj):
        return obj.get_unallocated_rent()

    def validate_bills_included(self, bills_included):
        if not bills_included:
            return bills_included
        VALID_BILL_CATEGORIES = {
            "electricity",
            "water",
            "gas",
            "internet",
            "waste",
            "heat",
            "cable",
            "sewer",
        }
        VALID_RESPONSIBILITY_TYPES = {"none", "percentage", "fixed", "full"}
        VALID_DISTRIBUTION_TYPES = {"none", "equal", "weighted", "custom"}
        for bill_key, bill_data in bills_included.items():
            if not isinstance(bill_data, dict):
                raise serializers.ValidationError(f"Bill {bill_key} must be an object")
            required_fields = ["included", "provider", "category"]
            for field in required_fields:
                if field not in bill_data:
                    raise serializers.ValidationError(
                        f"Bill {bill_key} is missing required field: {field}"
                    )
            if bill_data["category"] not in VALID_BILL_CATEGORIES:
                raise serializers.ValidationError(
                    f"Invalid category '{bill_data['category']}' for bill {bill_key}. "
                    f"Valid categories are: {', '.join(VALID_BILL_CATEGORIES)}"
                )
            if not bill_data.get("included", True):
                if "tenant_responsibility" not in bill_data:
                    raise serializers.ValidationError(
                        f"Bill {bill_key} is not included in rent but missing tenant_responsibility details"
                    )
                resp = bill_data["tenant_responsibility"]
                if not isinstance(resp, dict):
                    raise serializers.ValidationError(
                        f"tenant_responsibility for {bill_key} must be an object"
                    )
                if "type" not in resp:
                    raise serializers.ValidationError(
                        f"tenant_responsibility for {bill_key} missing 'type'"
                    )
                if resp["type"] not in VALID_RESPONSIBILITY_TYPES:
                    raise serializers.ValidationError(
                        f"Invalid responsibility type '{resp['type']}' for {bill_key}. "
                        f"Valid types are: {', '.join(VALID_RESPONSIBILITY_TYPES)}"
                    )
                if "distribution" not in resp:
                    raise serializers.ValidationError(
                        f"tenant_responsibility for {bill_key} missing 'distribution'"
                    )
                if resp["distribution"] not in VALID_DISTRIBUTION_TYPES:
                    raise serializers.ValidationError(
                        f"Invalid distribution type '{resp['distribution']}' for {bill_key}. "
                        f"Valid types are: {', '.join(VALID_DISTRIBUTION_TYPES)}"
                    )
                if resp["type"] != "none" and (
                    "value" not in resp or not isinstance(resp["value"], (int, float))
                ):
                    raise serializers.ValidationError(
                        f"tenant_responsibility for {bill_key} requires a numeric 'value'"
                    )
                if resp["type"] == "percentage" and (
                    resp["value"] < 0 or resp["value"] > 100
                ):
                    raise serializers.ValidationError(
                        f"Percentage value for {bill_key} must be between 0 and 100"
                    )
                if resp["distribution"] == "custom":
                    if "custom_splits" not in resp or not isinstance(
                        resp["custom_splits"], dict
                    ):
                        raise serializers.ValidationError(
                            f"Custom distribution for {bill_key} requires 'custom_splits' object"
                        )
                    splits_total = sum(resp["custom_splits"].values())
                    if abs(splits_total - 100) > 0.01:
                        raise serializers.ValidationError(
                            f"Custom splits for {bill_key} must add up to 100%, got {splits_total}%"
                        )
        return bills_included

    def validate_common_space_shared_with(self, value):
        if not value:
            return value
        valid_values = {c.value for c in Lease.CommonSpaceSharedWith}
        invalid = set(value) - valid_values
        if invalid:
            raise serializers.ValidationError(
                f"Invalid values: {invalid}. Valid options: {valid_values}"
            )
        return value

    def validate(self, data):
        property_obj = data.get("property", getattr(self.instance, "property", None))
        group_obj = data.get("group", getattr(self.instance, "group", None))
        try:
            instance_data = {
                **(self.instance.__dict__ if self.instance else {}),
                **data,
                "property": property_obj,
                "group": group_obj,
            }
            model_fields = {f.name for f in Lease._meta.get_fields()}
            cleaned_instance_data = {
                k: v for k, v in instance_data.items() if k in model_fields
            }
            temp_instance = Lease(**cleaned_instance_data)
            temp_instance.clean()
        except DjangoValidationError as e:
            raise serializers.ValidationError(serializers.as_serializer_error(e))
        except AttributeError:
            if not ((property_obj is None) ^ (group_obj is None)):
                raise serializers.ValidationError(
                    "Lease must link to EITHER property OR group."
                )
        return data

    @transaction.atomic
    def create(self, validated_data):
        from rentium.leases.services import create_lease_record

        landlord = validated_data.get("landlord")
        if landlord is None:
            raise serializers.ValidationError("Landlord context is required.")
        try:
            return create_lease_record(landlord=landlord, values=validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(serializers.as_serializer_error(exc)) from exc
