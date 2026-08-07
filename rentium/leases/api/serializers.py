import uuid
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
            "cleaning_deposit",
            "cleaning_deposit_paid",
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
            "cleaning_deposit_paid",
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
        # Read the current rent BEFORE the model-clean block below, which
        # setattr()s the incoming values straight onto self.instance. Comparing
        # after that point compares the new value with itself — which is why
        # the signed-rent rule that used to live further down never actually
        # fired on a real request.
        rent_before = (
            Decimal(self.instance.rent_amount) if self.instance else None
        )
        name_before = self.instance.invited_name if self.instance else None
        # Same trap, same reason: update() decides whether this is a REDIRECT,
        # and by then the instance is already carrying the new address.
        self._email_before = self.instance.invited_email if self.instance else None
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
        # --- Changing a signed tenant's rent share is an AMENDMENT ---
        # It used to be refused outright. It is allowed now, deliberately: the
        # landlord owns the document until it is executed, and refusing left
        # them with no route but Django admin. What it is not allowed to be is
        # silent — a signature is agreement to a specific number, so the change
        # writes an immutable TERMS_AMENDED event against that tenant. The
        # landlord decides whether to tell them; the record exists either way.
        # Once the lease is ACTIVE, LeaseNotLocked stops all of this anyway.
        if self.instance and self.instance.has_signed and "rent_amount" in data:
            if Decimal(str(data["rent_amount"])) != rent_before:
                self._amends_signed_rent = {
                    "before": str(rent_before),
                    "after": str(data["rent_amount"]),
                }
        # --- Invited name is frozen once the slot LINKS an account ---
        # Not once it signs. A landlord who typed "Siya Gulati" as "Sia Gulati"
        # has to be able to correct it before the lease is executed — that is
        # the name that prints in the parties and signature blocks, and being
        # unable to fix a typo was pushing people to delete and re-invite.
        # A linked account is different: from that point the tenant's own
        # account name is authoritative and invited_name is only a fallback,
        # so overwriting it would put a name on the agreement that the person
        # it names never chose.
        if self.instance and "invited_name" in data:
            # name_before, not self.instance.invited_name — the model-clean
            # block above has already written the incoming value onto the
            # instance, so comparing here compares the new value with itself.
            changed = (data["invited_name"] or "") != (name_before or "")
            if changed and self.instance.tenant_id:
                raise serializers.ValidationError(
                    {
                        "invited_name": (
                            "This tenant has linked their account, so their own "
                            "account name is used on the agreement from that point "
                            "and can't be overwritten here."
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

    def update(self, instance, validated_data):
        """Records the amendment when a signed tenant's rent share changes.

        Written after the save, and only if the save succeeded — an event
        claiming a change that a validation error rolled back would be worse
        than no event at all.
        """
        amendment = getattr(self, "_amends_signed_rent", None)

        # --- Redirecting an invite has to KILL the old link ---
        # The reason a landlord edits this field is that the invite went to the
        # wrong address. Changing the email alone left invite_token untouched,
        # so the stranger who received it kept a working link to read the
        # tenancy — names, rent, the property — and to sign it. Rotating the
        # token is what actually redirects the invite rather than just
        # relabelling it.
        #
        # Only for a slot nobody has claimed: once an account is linked the
        # token is not how they get in, and once they have signed the signature
        # is against this row and must not be disturbed.
        new_email = validated_data.get("invited_email")
        redirecting = (
            new_email
            and instance.tenant_id is None
            and not instance.has_signed
            and (new_email or "").casefold()
            != (getattr(self, "_email_before", None) or "").casefold()
        )
        if redirecting:
            validated_data["invite_token"] = uuid.uuid4()
            # Not sent to the new address yet. Leaving the old timestamp would
            # show the landlord a delivery they never made.
            validated_data["invite_sent_at"] = None
            # LINK_OPENED events are deliberately NOT cleared. If the wrong
            # recipient opened the invite, that happened, and it is exactly the
            # fact worth keeping — who saw the tenancy before it was redirected.

        updated = super().update(instance, validated_data)
        if redirecting:
            from rentium.leases.models import LeaseInviteEvent

            request = self.context.get("request")
            actor = getattr(request, "user", None)
            LeaseInviteEvent.objects.create(
                lease_tenant=updated,
                kind=LeaseInviteEvent.Kind.INVITE_REDIRECTED,
                actor=actor if getattr(actor, "pk", None) else None,
                metadata={
                    "from": getattr(self, "_email_before", None),
                    "to": updated.invited_email,
                    "old_link_revoked": True,
                },
            )
        if amendment:
            from rentium.leases.models import LeaseInviteEvent

            request = self.context.get("request")
            actor = getattr(request, "user", None)
            LeaseInviteEvent.objects.create(
                lease_tenant=updated,
                kind=LeaseInviteEvent.Kind.TERMS_AMENDED,
                actor=actor if getattr(actor, "pk", None) else None,
                metadata={
                    "fields": ["rent_amount"],
                    "before": {"rent_amount": amendment["before"]},
                    "after": {"rent_amount": amendment["after"]},
                    "signed_on": (
                        instance.signed_date.isoformat()
                        if instance.signed_date
                        else None
                    ),
                },
            )
        return updated


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
    property_furnishing_status = serializers.CharField(
        source="property.furnishing_status", read_only=True, allow_null=True
    )
    property_furnishing_details = serializers.CharField(
        source="property.furnishing_details", read_only=True, allow_null=True
    )
    property_furnishing_label = serializers.SerializerMethodField()
    # Shipped so the editor never hardcodes the enum — same reason the
    # inspection serializer ships its condition/cleanliness legends. These
    # drive which fair-use terms print on the agreement, so a frontend list
    # that drifted from the model would silently change the document.
    service_choices = serializers.SerializerMethodField()
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
            "property_furnishing_status",
            "property_furnishing_details",
            "property_furnishing_label",
            "service_choices",
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
            "cleaning_deposit",
            "security_deposit_received_date",
            "pet_deposit_received_date",
            "cleaning_deposit_received_date",
            "pets_allowed",
            "smoking_allowed",
            # AgreementTerms: DB columns that print into the agreement and were
            # previously reachable only from Django admin.
            "rent_due_day",
            "pets_terms",
            "smoking_terms",
            "parking_included",
            "parking_description",
            "parking_extra_charge",
            "services_and_facilities",
            "occupants",
            "house_rules",
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
            # A lease reaches ACTIVE only through check_and_activate(), which
            # freezes the signed document, posts the deposit and rent charges,
            # and opens occupancy. PATCHing status straight to ACTIVE skipped
            # all three and left an "active" lease with no charges behind it.
            # Status changes belong to landlord_sign / terminate / renew.
            "status",
            "status_display",
            "is_locked",
            "property_name",
            "property_address",
            "group_name",
            "bills_summary",
            "common_space_clause_text",
            "effective_landlord_contact",
            "effective_etransfer_email",
            "landlord_signed",
            "landlord_signed_date",
            # Stamped by the ledger when a deposit is actually settled — the
            # date money arrived is a fact, not a field to type over.
            "security_deposit_received_date",
            "pet_deposit_received_date",
            "cleaning_deposit_received_date",
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

    def get_service_choices(self, obj):
        from rentium.leases.agreement import ServiceOrFacility

        return [
            {"value": value, "label": str(label)}
            for value, label in ServiceOrFacility.choices
        ]

    def get_property_furnishing_label(self, obj):
        if not obj.property_id:
            return None
        from rentium.properties.furnishing import furnishing_label

        return furnishing_label(obj.property)

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

    def validate_co_hosts(self, value):
        """[{name, email?, phone?}] — a lease term like any other while the
        lease is unlocked, so it is editable rather than create-only.

        Note this is the LEGACY co-host list, which carries no signature. A
        co-landlord who must actually sign is a LeaseLandlordSignatory, invited
        through its own endpoint; nothing here grants that.
        """
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError(
                "Expected a list of {name, email, phone} objects."
            )
        cleaned = []
        for index, row in enumerate(value, start=1):
            if not isinstance(row, dict):
                raise serializers.ValidationError(
                    f"Entry {index} must be an object with at least a name."
                )
            name = str(row.get("name") or "").strip()
            if not name:
                raise serializers.ValidationError(f"Entry {index} needs a name.")
            cleaned.append(
                {
                    "name": name[:150],
                    "email": str(row.get("email") or "").strip()[:254],
                    "phone": str(row.get("phone") or "").strip()[:30],
                }
            )
        return cleaned

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

    @transaction.atomic
    def update(self, instance, validated_data):
        """Delegates to the one lease-edit service RAMA also uses.

        Both surfaces go through it so the amendment record written for tenants
        who already signed cannot be bypassed by editing from the other door.
        """
        from rentium.leases.services import update_lease_record

        request = self.context.get("request")
        try:
            result = update_lease_record(
                landlord=instance.landlord,
                lease=instance,
                values=validated_data,
                actor=getattr(request, "user", None),
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(serializers.as_serializer_error(exc)) from exc
        # Surfaced on the response so the landlord's UI can say WHO signed under
        # the old terms rather than silently succeeding.
        self._amended_signers = result["amended_signers"]
        return result["lease"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        amended = getattr(self, "_amended_signers", None)
        if amended is not None:
            data["amended_signers"] = amended
        return data
