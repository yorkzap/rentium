import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rentium.core.phone import PhoneField
from rentium.properties.models import Property
from rentium.properties.models import PropertyGroup
from rentium.users.models import LandlordProfile
from rentium.users.models import TenantProfile

from .agreement import AgreementTerms
from .inspections import AreaConditionState  # noqa: E402,F401
from .inspections import ConditionInspection  # noqa: E402,F401
from .inspections import DepositDeduction  # noqa: E402,F401
from .inspections import InspectionItem  # noqa: E402,F401
from .inspections import InspectionKeyRow  # noqa: E402,F401
from .inspections import InspectionTemplate  # noqa: E402,F401
from .inspections import InspectionTemplateItem  # noqa: E402,F401
from .moveout import MoveOutRequest  # noqa: E402,F401
from .occupancy import Occupancy  # noqa: E402,F401


class Lease(AgreementTerms):
    class LeaseType(models.TextChoices):
        # --- Retired for NEW leases, kept only so existing leases created
        # before the "one Standard Roommate Agreement for all rooms,
        # regardless of province" change keep a valid, readable value.
        # lease_types_view() no longer offers these two for new leases.
        BC_ROOMMATE_AGREEMENT = "BC_ROOMMATE", _("BC TRAC Roommate Agreement")
        SK_ROOMMATE_AGREEMENT = "SK_ROOMMATE", _("Saskatchewan Roommate Agreement")
        # Complete-unit agreements stay province-specific.
        BC_RESIDENTIAL_TENANCY = "BC_RESIDENTIAL", _("BC Residential Tenancy (RTB-1)")
        SK_RESIDENTIAL_TENANCY = "SK_RESIDENTIAL", _("Saskatchewan Residential Tenancy")
        GENERIC_RESIDENTIAL = "GENERIC_RESIDENTIAL", _("Standard Residential Agreement")
        # The one roommate/room agreement offered for all NEW room leases,
        # in every province — see lease_types_view() in api/views.py.
        GENERIC_ROOMMATE = "GENERIC_ROOMMATE", _("Standard Roommate Agreement")

    class LeaseStatus(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        PENDING_SIGNATURES = "PENDING", _("Pending Signatures")
        ACTIVE = "ACTIVE", _("Active")
        EXPIRED = "EXPIRED", _("Expired")
        TERMINATED = "TERMINATED", _("Terminated")
        RENEWED = "RENEWED", _("Renewed")

    class CommonSpaceSharedWith(models.TextChoices):
        """Used inside the JSON list on `common_space_shared_with`."""

        ROOMMATES = "ROOMMATES", _("Other Roommates")
        LANDLORD = "LANDLORD", _("The Landlord")
        LANDLORD_RELATIVES = "LANDLORD_RELATIVES", _("The Landlord's Relatives")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease_type = models.CharField(
        _("Lease Type"), max_length=25, choices=LeaseType.choices
    )
    property = models.ForeignKey(
        Property,
        on_delete=models.PROTECT,
        related_name="leases",
        null=True,
        blank=True,
        help_text=_("Link to a specific property (Room or Complete Unit)"),
    )
    group = models.ForeignKey(
        PropertyGroup,
        on_delete=models.PROTECT,
        related_name="group_leases",
        null=True,
        blank=True,
        help_text=_(
            "Link to a group for shared accommodation agreements covering multiple rooms"
        ),
    )
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.PROTECT, related_name="landlord_leases"
    )
    # Co-hosts / co-landlords recorded ON THIS AGREEMENT — additional landlord
    # parties (a partner, a co-owner, a property manager) shown on the document
    # and reachable for notice. A list of {"name", "email", "phone"} dicts. This
    # is a RECORD on the lease, not an app login/permission grant.
    co_hosts = models.JSONField(_("Co-hosts"), default=list, blank=True)
    # Common lease fields
    lease_number = models.CharField(
        _("Lease Number"), max_length=20, unique=True, blank=True, editable=False
    )
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=LeaseStatus.choices,
        default=LeaseStatus.DRAFT,
    )
    start_date = models.DateField(_("Start Date"))
    end_date = models.DateField(
        _("End Date"), null=True, blank=True, help_text=_("Blank for month-to-month")
    )
    is_month_to_month = models.BooleanField(_("Month-to-Month"), default=False)
    move_in_date = models.DateField(_("Move-in Date"), null=True, blank=True)
    move_out_date = models.DateField(_("Move-out Date"), null=True, blank=True)
    # Financial details
    security_deposit = models.DecimalField(
        _("Security Deposit"), max_digits=10, decimal_places=2, default=0
    )
    pet_deposit = models.DecimalField(
        _("Pet Deposit"), max_digits=10, decimal_places=2, default=0
    )
    cleaning_deposit = models.DecimalField(
        _("Cleaning Deposit (Overall Lease)"),
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text=_(
            "Refundable cleaning deposit for the lease; individual roommate "
            "deposits may be recorded on LeaseTenant"
        ),
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
        ),
    )
    special_terms = models.TextField(_("Special Terms"), blank=True)
    # --- Total rent (authoritative) ---
    # This is the single source of truth for what the whole lease costs per
    # month. Individual LeaseTenant.rent_amount values are each tenant's
    # share of THIS number, not independent figures — see
    # get_unallocated_rent() and the tenant-facing rent display rules in
    # LeaseTenant docstrings below for why tenants are shown this figure,
    # not their individual share.
    total_rent = models.DecimalField(
        _("Total Monthly Rent"),
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text=_(
            "The full monthly rent for the unit/room(s) under this lease. "
            "Individual tenants' rent_amount values should sum to this."
        ),
    )
    # --- Room / shared-accommodation clause ---
    common_space_shared_with = models.JSONField(
        _("Common Space Shared With"),
        default=list,
        blank=True,
        help_text=_(
            "Only relevant for ROOMMATE-type leases. Subset of ['ROOMMATES', 'LANDLORD', "
            "'LANDLORD_RELATIVES'] describing who else may use the shared common spaces. "
            "Rendered into the lease document's shared-space clause."
        ),
    )
    # --- RTB-1 style landlord address-for-service / contact block ---
    # Falls back to the property's landlord contact info at render time if left blank.
    landlord_service_address = models.CharField(
        _("Landlord Address for Service"),
        max_length=255,
        blank=True,
        help_text=_("Defaults to the rental unit/property address if left blank"),
    )
    landlord_daytime_phone = PhoneField(_("Landlord Daytime Phone"))
    landlord_other_phone = PhoneField(_("Landlord Other Phone"))
    landlord_fax = models.CharField(_("Landlord Fax"), max_length=20, blank=True)
    landlord_service_email = models.EmailField(
        _("Landlord Email for Service"), blank=True
    )
    # --- Payment instructions ---
    etransfer_email = models.EmailField(
        _("e-Transfer Email"),
        blank=True,
        help_text=_(
            "Where tenants should send their e-transfers for this lease. Shown "
            "in the tenant dashboard's 'Make a Payment' section. Falls back to "
            "the landlord service email, then the landlord's account email — "
            "see get_effective_etransfer_email()."
        ),
    )
    # --- Move-out notice terms (see leases/tenancy_rules.py) ---
    custom_tenant_notice_months = models.PositiveSmallIntegerField(
        _("Tenant Notice Period (months)"),
        default=1,
        help_text=_(
            "Months of notice the tenant must give to end this tenancy. "
            "Honored ONLY when the provincial tenancy act does not apply — "
            "i.e. the landlord (or their relatives) shares kitchen/common "
            "areas with the tenancy (RTA s.4(c) exemption). When the act "
            "applies, its statutory minimums override this."
        ),
    )
    # --- Fixed-term end / notice-to-vacate clause (RTB-1 section E) ---
    fixed_term_end_reason = models.TextField(
        _("Reason Tenant Must Vacate"),
        blank=True,
        help_text=_(
            "Required only if this lease ends with a mandatory vacate clause (RTA s.13.1 or sublease)"
        ),
    )
    fixed_term_end_regulation_section = models.CharField(
        _("Residential Tenancy Regulation Section"), max_length=20, blank=True
    )
    # Document handling
    document_file = models.FileField(
        _("Main Agreement Document"),
        upload_to="lease_documents/%Y/%m/",
        null=True,
        blank=True,
    )
    # If lease was renewed/replaced
    previous_lease = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="renewal_leases",
        help_text=_("Link to the lease this one renewed/replaced"),
    )
    # --- Signed-document snapshot (immutability) ---
    # Captured once, at activation, by documents.capture_signed_document. It
    # freezes the rendered agreement TERMS so that later edits to clauses.py can
    # never retroactively change what a tenant signed; the SHA-256 is stored for
    # tamper-evidence. Not editable via API/admin — a signed record is immutable,
    # the same principle as the append-only ledger.
    signed_document = models.JSONField(
        _("Signed Document Snapshot"), null=True, blank=True, editable=False
    )
    signed_document_sha256 = models.CharField(
        _("Signed Document Checksum"),
        max_length=64,
        blank=True,
        default="",
        editable=False,
    )
    # --- Signature / execution tracking ---
    landlord_signed = models.BooleanField(_("Landlord Signed"), default=False)
    landlord_signed_date = models.DateTimeField(
        _("Date Landlord Signed"), null=True, blank=True
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
            return (
                f"{self.get_lease_type_display()} - {self.property.name} ({lease_id})"
            )
        elif self.group:
            return f"{self.get_lease_type_display()} - Group: {self.group.name} ({lease_id})"
        return f"{self.get_lease_type_display()} ({lease_id})"

    def clean(self):
        super().clean()
        # Validate that either property OR group is set, but not both or neither
        if (self.property and self.group) or (not self.property and not self.group):
            raise ValidationError(
                _(
                    "A lease must be associated with either a property OR a property group, not both or neither."
                )
            )
        # Validate lease type matches property category
        is_roommate_type = "ROOMMATE" in self.lease_type
        is_residential_type = "RESIDENTIAL" in self.lease_type
        if self.property:
            if (
                is_roommate_type
                and self.property.property_category != Property.PropertyCategory.ROOM
            ):
                raise ValidationError(
                    _("Roommate agreement types can only be used with Room properties.")
                )
            if (
                is_residential_type
                and self.property.property_category
                != Property.PropertyCategory.COMPLETE_UNIT
            ):
                raise ValidationError(
                    _(
                        "Residential agreement types can only be used with Complete Unit properties."
                    )
                )
        if self.group and not is_roommate_type:
            raise ValidationError(
                _(
                    "Leases linked to a Property Group must be a Roommate agreement type."
                )
            )
        self._validate_no_cross_scope_overlap()
        # Ensure landlord consistency
        if self.property and self.property.landlord != self.landlord:
            raise ValidationError(
                _("The landlord must own the property associated with this lease.")
            )
        if self.group and self.group.landlord != self.landlord:
            raise ValidationError(
                _(
                    "The landlord must own the property group associated with this lease."
                )
            )
        # End date validation. A month-to-month lease has no end date while it
        # runs — but terminating one legitimately SETS an end date (the UI's
        # terminate endpoint has always done this), so the rule only applies
        # to non-final statuses.
        final_statuses = (
            self.LeaseStatus.TERMINATED,
            self.LeaseStatus.EXPIRED,
            self.LeaseStatus.RENEWED,
        )
        if (
            self.is_month_to_month
            and self.end_date
            and self.status not in final_statuses
        ):
            raise ValidationError(
                _("Month-to-month leases should not have an end date.")
            )
        if not self.is_month_to_month and not self.end_date:
            raise ValidationError(_("Fixed-term leases must have an end date."))
        # Start/End date logic
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError(_("End date cannot be before the start date."))
        if (
            self.move_out_date
            and self.move_in_date
            and self.move_out_date < self.move_in_date
        ):
            raise ValidationError(_("Move-out date cannot be before the move-in date."))
        if self.move_in_date and self.move_in_date < self.start_date:
            raise ValidationError(
                _("Move-in date cannot be before the lease start date.")
            )
        # Common space clause only makes sense for roommate-type leases
        if self.common_space_shared_with and not is_roommate_type:
            raise ValidationError(
                _("common_space_shared_with only applies to Roommate agreement types.")
            )
        if self.common_space_shared_with:
            valid_values = {c.value for c in Lease.CommonSpaceSharedWith}
            invalid = set(self.common_space_shared_with) - valid_values
            if invalid:
                raise ValidationError(
                    _(f"Invalid common_space_shared_with values: {invalid}")
                )
        # Vacate-reason clause requires the regulation section (or vice versa isn't required)
        if self.fixed_term_end_reason and not self.fixed_term_end_regulation_section:
            # Not fatal on its own (sublease agreements don't need a regulation section),
            # but flagged here as a soft reminder point if you want to enforce it later.
            pass
        # Validate bills_included structure if provided
        if self.bills_included:
            self._validate_bills_included()

    def _validate_no_cross_scope_overlap(self):
        """A unit cannot be let whole and by the room at the same time.

        The two scopes describe the same physical space, so overlapping them
        double-lets it: a family renting the whole floor and a roommate renting
        Bedroom 2 both hold a valid agreement to the same bedroom. Checked here
        (rather than only at mode-switch time) because a lease can be created
        long after a switch, and re-checked on activation.

        Only live leases conflict — terminated and expired ones are history.
        """
        from rentium.properties.models import Property

        live = ("DRAFT", "PENDING", "ACTIVE")
        if self.status not in live:
            return

        unit_id = None
        if self.property_id and self.property.unit_id:
            unit_id = self.property.unit_id
        elif self.group_id and getattr(self.group, "unit_id", None):
            unit_id = self.group.unit_id
        if unit_id is None:
            return

        covers_whole = bool(
            self.property_id
            and self.property.property_category
            == Property.PropertyCategory.COMPLETE_UNIT
        )

        others = Lease.objects.filter(status__in=live).exclude(pk=self.pk)
        others = others.filter(
            models.Q(property__unit_id=unit_id) | models.Q(group__unit_id=unit_id)
        )
        for other in others.select_related("property"):
            other_covers_whole = bool(
                other.property_id
                and other.property.property_category
                == Property.PropertyCategory.COMPLETE_UNIT
            )
            if covers_whole != other_covers_whole:
                whole, room = (
                    (self, other) if covers_whole else (other, self)
                )
                raise ValidationError(
                    _(
                        "This unit already has a live %(kind)s lease (%(number)s). "
                        "A unit cannot be rented whole and by the room at the "
                        "same time — end that lease first, or change what this "
                        "one covers."
                    )
                    % {
                        "kind": "whole-unit" if other_covers_whole else "room",
                        "number": other.lease_number,
                    }
                )

    def _validate_bills_included(self):
        """Validate the structure and values in the bills_included field."""
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
        for bill_key, bill_data in self.bills_included.items():
            if not isinstance(bill_data, dict):
                raise ValidationError(_(f"Bill {bill_key} must be an object"))
            required_fields = ["included", "provider", "category"]
            for field in required_fields:
                if field not in bill_data:
                    raise ValidationError(
                        _(f"Bill {bill_key} is missing required field: {field}")
                    )
            if bill_data["category"] not in VALID_BILL_CATEGORIES:
                raise ValidationError(
                    _(
                        f"Invalid category '{bill_data['category']}' for bill {bill_key}. "
                        f"Valid categories are: {', '.join(VALID_BILL_CATEGORIES)}"
                    )
                )
            if not bill_data.get("included", True):
                if "tenant_responsibility" not in bill_data:
                    raise ValidationError(
                        _(
                            f"Bill {bill_key} is not included in rent but missing tenant_responsibility details"
                        )
                    )
                resp = bill_data["tenant_responsibility"]
                if not isinstance(resp, dict):
                    raise ValidationError(
                        _(f"tenant_responsibility for {bill_key} must be an object")
                    )
                if "type" not in resp:
                    raise ValidationError(
                        _(f"tenant_responsibility for {bill_key} missing 'type'")
                    )
                if resp["type"] not in VALID_RESPONSIBILITY_TYPES:
                    raise ValidationError(
                        _(
                            f"Invalid responsibility type '{resp['type']}' for {bill_key}. "
                            f"Valid types are: {', '.join(VALID_RESPONSIBILITY_TYPES)}"
                        )
                    )
                if "distribution" not in resp:
                    raise ValidationError(
                        _(
                            f"tenant_responsibility for {bill_key} missing 'distribution'"
                        )
                    )
                if resp["distribution"] not in VALID_DISTRIBUTION_TYPES:
                    raise ValidationError(
                        _(
                            f"Invalid distribution type '{resp['distribution']}' for {bill_key}. "
                            f"Valid types are: {', '.join(VALID_DISTRIBUTION_TYPES)}"
                        )
                    )
                if resp["type"] != "none" and (
                    "value" not in resp or not isinstance(resp["value"], (int, float))
                ):
                    raise ValidationError(
                        _(
                            f"tenant_responsibility for {bill_key} requires a numeric 'value'"
                        )
                    )
                if resp["type"] == "percentage" and (
                    resp["value"] < 0 or resp["value"] > 100
                ):
                    raise ValidationError(
                        _(f"Percentage value for {bill_key} must be between 0 and 100")
                    )
                if resp["distribution"] == "custom":
                    if "custom_splits" not in resp or not isinstance(
                        resp["custom_splits"], dict
                    ):
                        raise ValidationError(
                            _(
                                f"Custom distribution for {bill_key} requires 'custom_splits' object"
                            )
                        )
                    splits_total = sum(resp["custom_splits"].values())
                    if abs(splits_total - 100) > 0.01:
                        raise ValidationError(
                            _(
                                f"Custom splits for {bill_key} must add up to 100%, got {splits_total}%"
                            )
                        )

    def save(self, *args, **kwargs):
        if not self.lease_number:
            timestamp = int(timezone.now().timestamp())
            random_suffix = uuid.uuid4().hex[:4].upper()
            prefix = "RMT" if "ROOMMATE" in self.lease_type else "RES"
            self.lease_number = f"{prefix}{timestamp % 1000000}-{random_suffix}"
        self.full_clean()
        super().save(*args, **kwargs)

    # --- Lease locking ---
    def is_locked(self):
        """
        Once a lease is fully executed (or beyond), API-level edits are blocked.
        Only Django admin can touch it after this point.
        """
        return self.status in [
            Lease.LeaseStatus.ACTIVE,
            Lease.LeaseStatus.EXPIRED,
            Lease.LeaseStatus.TERMINATED,
            Lease.LeaseStatus.RENEWED,
        ]

    def check_and_activate(self):
        """
        Call after any signature event.
        --- Joint-and-several activation policy ---
        This lease activates once the landlord AND AT LEAST ONE named tenant
        have signed — not once EVERY tenant has signed. This is deliberate,
        not a shortcut:
        1. The lease binds all named tenants jointly and severally: each
           tenant is independently on the hook for the FULL rent, not just
           their individual share, regardless of who else has or hasn't
           signed. A tenant who signs is agreeing to that full exposure.
        2. Requiring every roommate to sign before anyone can move in or the
           lease takes effect would let a single holdout block the whole
           household indefinitely, which doesn't match how shared-room
           tenancies actually work in practice.
        3. Tenants who haven't signed yet are NOT removed from the lease and
           are NOT prevented from signing later — LeaseTenant.sign() and the
           `sign` API action both remain available after the lease is
           ACTIVE (see Lease.accepts_signatures()). Once they do sign,
           they're retroactively bound by the same agreement everyone else
           already accepted; nothing about the lease terms changes because
           they signed "late."
        4. A tenant who never signs is still nominally on the lease record
           (useful for record-keeping / disputes) but has not personally
           agreed to anything — landlords should treat an unsigned
           LeaseTenant as "not a confirmed occupant" for practical purposes
           (e.g. don't hand them keys) even though the lease itself is
           active.
        This is a product/business policy encoded in software, not legal
        advice — whether "jointly and severally binding once any one tenant
        and the landlord sign" holds up as intended depends on your
        jurisdiction and the actual lease document text. Confirm this
        matches the legal agreement's own wording before relying on it.
        Idempotent / safe to call repeatedly.
        """
        if self.status != Lease.LeaseStatus.PENDING_SIGNATURES:
            return False
        any_tenant_signed = self.lease_tenants.filter(has_signed=True).exists()
        # Every additional co-landlord on the lease must also have signed —
        # the owner alone can't activate a lease that names co-signers.
        all_co_landlords_signed = not self.landlord_signatories.filter(
            has_signed=False
        ).exists()
        if self.landlord_signed and all_co_landlords_signed and any_tenant_signed:
            self.status = Lease.LeaseStatus.ACTIVE
            self.save(update_fields=["status", "updated_at"])
            self.clip_overlapping_month_to_month_leases()
            # --- Ledger + occupancy + event (deferred imports avoid cycles) ---
            # Runs exactly once: the guard at the top of this method returns
            # early unless status was PENDING, and generation is idempotent.
            from rentium.events.registry import publish
            from rentium.leases.documents import capture_signed_document
            from rentium.leases.occupancy import open_occupancy
            from rentium.ledger.billing import generate_initial_charges

            # Freeze the agreement as signed, before anything else reads it, so
            # later clause edits can never change this executed lease's document.
            capture_signed_document(self)
            generate_initial_charges(self)  # deposits, fees, prorated rent schedule
            for lt in self.lease_tenants.filter(tenant__isnull=False, declined=False):
                open_occupancy(lt)  # start the "who lived where when" log
            publish(
                "lease.activated",
                {"lease_id": str(self.pk)},
                property_id=self.property_id,
                lease_id=self.pk,
            )
            return True
        return False

    def accepts_signatures(self):
        """
        Whether a tenant can still sign this lease right now. Deliberately
        broader than "not is_locked()" — ACTIVE leases still accept
        signatures from tenants who haven't signed yet (see
        check_and_activate() docstring on joint-and-several activation).
        Only a lease that has moved past ACTIVE entirely (expired,
        terminated, or superseded by a renewal) stops accepting them.
        """
        return self.status in [
            Lease.LeaseStatus.PENDING_SIGNATURES,
            Lease.LeaseStatus.ACTIVE,
        ]

    def get_unallocated_rent(self):
        """
        total_rent minus the sum of what's currently assigned across
        LeaseTenant.rent_amount. Positive = still needs to be assigned to
        someone; zero = fully allocated; negative = over-allocated (the
        tenant rows currently sum to more than total_rent — a data problem
        worth flagging to the landlord, not silently allowed).
        """
        allocated = self.get_total_monthly_rent()
        return (self.total_rent or Decimal("0.00")) - Decimal(allocated)

    def rent_is_fully_allocated(self):
        """
        True if there's nothing left to assign. Leases with total_rent left
        at its default of 0 are treated as "not using this feature" and are
        always considered fine (the old, pre-total_rent way of setting each
        tenant's rent_amount independently still works without this gate).
        This is the actual enforcement point for "the total has to add up
        before anyone can sign" — see the `sign` / `landlord_sign` actions,
        which refuse to proceed while this is False. Deliberately checked
        at signing rather than at every tenant create/update, so a
        landlord can still invite people one at a time over several days
        without the system complaining about a normal, temporary
        in-progress state — it only insists on being correct by the moment
        it actually matters.
        """
        if not self.total_rent or self.total_rent <= 0:
            return True
        return abs(self.get_unallocated_rent()) <= Decimal("0.01")

    def get_overlapping_leases(self, exclude_statuses=None):
        """
        Read-only lookup used to WARN a landlord at lease-creation time,
        not to auto-resolve anything. Returns other leases on the same
        property (or group) whose date range overlaps this one's.
        By default only ACTIVE and PENDING_SIGNATURES leases are considered
        "in the way" — DRAFT leases aren't real commitments yet, and
        EXPIRED/TERMINATED/RENEWED ones are already resolved. Pass
        exclude_statuses to narrow further if needed.
        """
        if not self.property and not self.group:
            return Lease.objects.none()
        default_relevant_statuses = [
            Lease.LeaseStatus.ACTIVE,
            Lease.LeaseStatus.PENDING_SIGNATURES,
        ]
        candidates = Lease.objects.filter(status__in=default_relevant_statuses)
        if self.pk:
            candidates = candidates.exclude(pk=self.pk)
        if exclude_statuses:
            candidates = candidates.exclude(status__in=exclude_statuses)
        if self.property:
            candidates = candidates.filter(property=self.property)
        else:
            candidates = candidates.filter(group=self.group)
        return [other for other in candidates if self._overlaps(other)]

    def clip_overlapping_month_to_month_leases(self):
        """
        Called once THIS lease goes ACTIVE. Deliberately narrow: it does
        NOT void, terminate, or otherwise touch fixed-term leases that
        happen to overlap — those are real, dated commitments, and any
        genuine conflict there should have already been surfaced as a
        warning at creation time (see get_overlapping_leases()) for a human
        to resolve, not silently rewritten by the system.
        The one case this DOES handle automatically: an existing ACTIVE
        month-to-month lease on the same property/group. Month-to-month
        leases have no end_date by definition, so they'd overlap literally
        every future lease on the same space forever — that's not a real
        conflict to flag, it's just what "month-to-month" means. Instead of
        voiding it, this simply gives it the end_date it was always going
        to need eventually: the day the new lease starts. Its status is
        left alone (still ACTIVE — it just now has a defined end rather
        than running indefinitely), and is_month_to_month flips to False
        since it's no longer open-ended.
        Editing an already-ACTIVE (locked) lease's end_date here is an
        intentional exception to the normal "only Django admin edits a
        locked lease" rule — this is a system-driven consequence of the new
        lease's own activation, not a manual bypass of the lock.
        """
        if not self.property and not self.group:
            return
        candidates = Lease.objects.filter(
            status=Lease.LeaseStatus.ACTIVE,
            is_month_to_month=True,
        ).exclude(pk=self.pk)
        if self.property:
            candidates = candidates.filter(property=self.property)
        else:
            candidates = candidates.filter(group=self.group)
        for other in candidates:
            if not self._overlaps(other):
                continue
            other.is_month_to_month = False
            other.end_date = self.start_date
            note = (
                f"[System] End date set to {self.start_date} on "
                f"{timezone.now().date()} because lease {self.lease_number} "
                f"became active for the same property, starting that day."
            )
            other.special_terms = (
                f"{other.special_terms}\n\n{note}".strip()
                if other.special_terms
                else note
            )
            other.save()

    def _overlaps(self, other):
        """True if this lease's [start_date, end_date] range overlaps
        other's, treating a null end_date as open-ended (extends forever)."""
        this_end = self.end_date
        other_end = other.end_date
        starts_before_other_ends = other_end is None or self.start_date <= other_end
        other_starts_before_this_ends = this_end is None or other.start_date <= this_end
        return starts_before_other_ends and other_starts_before_this_ends

    def get_effective_landlord_contact(self):
        """Resolves landlord service-block fields, falling back to the linked property/user."""
        landlord_user = self.landlord.user
        base_address = (
            self.property.address
            if self.property
            else (self.group.name if self.group else "")
        )
        return {
            "address": self.landlord_service_address or base_address,
            "daytime_phone": self.landlord_daytime_phone or self.landlord.user.phone,
            "other_phone": self.landlord_other_phone,
            "fax": self.landlord_fax,
            "email": self.landlord_service_email or landlord_user.email,
        }

    def get_effective_etransfer_email(self):
        """Where tenants should send money for THIS lease, with fallbacks so
        the tenant dashboard always has something to show."""
        return (
            self.etransfer_email
            or self.landlord_service_email
            or self.landlord.user.email
        )

    def get_common_space_clause_text(self):
        """Human-readable rendering of common_space_shared_with for the document template."""
        if not self.common_space_shared_with:
            return ""
        labels = {
            "ROOMMATES": "other roommates",
            "LANDLORD": "the landlord",
            "LANDLORD_RELATIVES": "the landlord's relatives",
        }
        parts = [labels[v] for v in self.common_space_shared_with if v in labels]
        if not parts:
            return ""
        return (
            "The common spaces of the Property may be shared with "
            + ", ".join(parts)
            + "."
        )

    def get_total_monthly_rent(self):
        """Calculates total monthly rent from associated LeaseTenant records (base rent, pre-adjustment)."""
        total = self.lease_tenants.aggregate(total=Sum("rent_amount"))["total"]
        return total or 0

    def get_current_tenant_count(self):
        """Gets the current number of tenants associated with this lease."""
        return self.lease_tenants.count()

    def get_max_occupancy(self):
        """Gets the maximum occupancy based on the linked property or group."""
        if self.property:
            if (
                self.property.property_category
                == Property.PropertyCategory.COMPLETE_UNIT
            ):
                return self.property.max_occupancy or 1
            elif self.property.property_category == Property.PropertyCategory.ROOM:
                return 1
        elif self.group:
            return self.group.grouped_properties.filter(
                property_category=Property.PropertyCategory.ROOM
            ).count()
        return 0

    # Display order / labels for bills_included keys (UI + PDF + summary).
    BILL_CATEGORY_LABELS = {
        "electricity": "Electricity",
        "water": "Water",
        "heat": "Heat",
        "gas": "Gas",
        "internet": "Internet",
        "cable": "Cable / TV",
        "waste": "Garbage / Recycling",
        "sewer": "Sewer",
    }

    def get_bills_summary(self):
        """Returns a human-readable summary of bills and tenant responsibilities.

        Always names the utility (Electricity, Water, …). Provider is optional
        context in parentheses — never the only label (empty provider used to
        render as \" - Included in rent\").
        """
        if not self.bills_included:
            return "No bills information available"
        summaries = []
        # Stable order: known categories first, then any custom keys.
        keys = list(self.BILL_CATEGORY_LABELS.keys())
        for key in self.bills_included:
            if key not in keys:
                keys.append(key)
        for bill_type in keys:
            details = self.bills_included.get(bill_type)
            if not isinstance(details, dict):
                continue
            label = self.BILL_CATEGORY_LABELS.get(
                bill_type, str(bill_type).replace("_", " ").title()
            )
            provider = (details.get("provider") or "").strip()
            name = f"{label} ({provider})" if provider else label
            if details.get("included", False):
                summaries.append(f"{name} — included in rent")
            else:
                resp = details.get("tenant_responsibility") or {}
                if not isinstance(resp, dict):
                    resp = {}
                resp_type = resp.get("type")
                if resp_type == "full":
                    summaries.append(f"{name} — tenant pays 100%")
                elif resp_type == "percentage":
                    value = resp.get("value", 0)
                    summaries.append(f"{name} — tenant pays {value}%")
                elif resp_type == "fixed":
                    value = resp.get("value", 0)
                    summaries.append(f"{name} — tenant pays ${value}/month")
                else:
                    note = (details.get("notes") or "").strip()
                    if note:
                        summaries.append(f"{name} — {note}")
                    else:
                        summaries.append(f"{name} — tenant-paid")
        if not summaries:
            return "No bills information available"
        return "; ".join(summaries)

    def calculate_tenant_bill_share(self, tenant_id, bill_type, bill_amount):
        """Calculates a specific tenant's share of a given bill."""
        if not self.bills_included or bill_type not in self.bills_included:
            return Decimal("0.00")
        bill_details = self.bills_included[bill_type]
        if bill_details.get("included", True):
            return Decimal("0.00")
        resp = bill_details.get("tenant_responsibility", {})
        resp_type = resp.get("type")
        if not resp_type or resp_type == "none":
            return Decimal("0.00")
        tenant_ids = [
            str(lt.tenant.id) for lt in self.lease_tenants.all() if lt.tenant_id
        ]
        tenant_count = len(tenant_ids)
        if tenant_count == 0:
            return Decimal("0.00")
        tenant_share = Decimal("0.00")
        if resp_type == "full":
            tenant_portion = Decimal(bill_amount)
        elif resp_type == "percentage":
            percentage = Decimal(resp.get("value", 0)) / Decimal("100.0")
            tenant_portion = bill_amount * percentage
        elif resp_type == "fixed":
            fixed_amount = Decimal(resp.get("value", 0))
            distribution = resp.get("distribution")
            if distribution == "custom":
                custom_splits = resp.get("custom_splits", {})
                tenant_percentage = Decimal(
                    custom_splits.get(str(tenant_id), 0)
                ) / Decimal("100.0")
                return fixed_amount * tenant_percentage
            elif distribution == "equal":
                return fixed_amount / Decimal(tenant_count)
            elif distribution == "weighted":
                return fixed_amount / Decimal(tenant_count)
            else:
                return fixed_amount
        else:
            return Decimal("0.00")
        distribution = resp.get("distribution")
        if distribution == "equal":
            tenant_share = tenant_portion / Decimal(tenant_count)
        elif distribution == "custom":
            custom_splits = resp.get("custom_splits", {})
            tenant_percentage = Decimal(custom_splits.get(str(tenant_id), 0)) / Decimal(
                "100.0"
            )
            tenant_share = tenant_portion * tenant_percentage
        elif distribution == "weighted":
            total_rent = self.get_total_monthly_rent()
            if total_rent > 0:
                try:
                    tenant_lease = self.lease_tenants.get(tenant__id=tenant_id)
                    tenant_rent = tenant_lease.rent_amount
                    tenant_share = tenant_portion * (tenant_rent / total_rent)
                except Exception:
                    tenant_share = tenant_portion / Decimal(tenant_count)
            else:
                tenant_share = tenant_portion / Decimal(tenant_count)
        else:
            tenant_share = tenant_portion / Decimal(tenant_count)
        return tenant_share.quantize(Decimal("0.01"))


class LeaseTenant(models.Model):
    """
    Links a TenantProfile to a Lease, specifying their individual terms.
    `tenant` is nullable to support inviting someone by email/phone before they
    have (or have claimed) a TenantProfile. The invite_token lets them view/sign
    the lease via a direct link, and the account gets linked automatically once
    they sign up with (or already have) a matching email.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(
        Lease, on_delete=models.CASCADE, related_name="lease_tenants"
    )
    tenant = models.ForeignKey(
        TenantProfile,
        on_delete=models.PROTECT,
        related_name="tenant_leases",
        null=True,
        blank=True,
        help_text=_(
            "Set once the invited person has a linked account. Null while invite is pending."
        ),
    )
    rent_amount = models.DecimalField(
        _("Individual Monthly Rent"),
        max_digits=10,
        decimal_places=2,
        help_text=_(
            "This tenant's share of Lease.total_rent — used for internal "
            "accounting/splits only. Per the joint-and-several policy (see "
            "Lease.check_and_activate()), tenants should always be shown "
            "Lease.total_rent as 'their' rent in tenant-facing UI, never "
            "this per-tenant share, so nobody is nudged toward paying only "
            "a partial amount."
        ),
    )
    # For roommate agreements, link to specific room if applicable
    room = models.ForeignKey(
        Property,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="room_tenants",
        limit_choices_to={"property_category": Property.PropertyCategory.ROOM},
        help_text=_(
            "Specific room assignment within a group lease (Roommate Agreements only)"
        ),
    )
    # Individual refundable cleaning deposit for roommate agreements
    cleaning_deposit = models.DecimalField(
        _("Individual Cleaning Deposit"),
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text=_(
            "Cleaning deposit charged specifically to this tenant (for roommate leases)"
        ),
    )
    cleaning_deposit_paid = models.BooleanField(
        _("Cleaning Deposit Paid"), default=False
    )
    is_primary_tenant = models.BooleanField(
        _("Primary Tenant"),
        default=False,
        help_text=_(
            "Is this the primary contact for communications regarding the lease?"
        ),
    )
    has_signed = models.BooleanField(_("Has Signed Agreement"), default=False)
    signed_date = models.DateTimeField(_("Date Signed"), null=True, blank=True)
    declined = models.BooleanField(_("Declined"), default=False)
    declined_at = models.DateTimeField(_("Date Declined"), null=True, blank=True)
    decline_reason = models.TextField(_("Decline Reason"), blank=True)
    # Individual tenant dates (if they differ from main lease, e.g., late joiner)
    individual_start_date = models.DateField(
        _("Individual Start Date"),
        null=True,
        blank=True,
        help_text=_("Tenant's specific start date if different from lease.start_date"),
    )
    individual_end_date = models.DateField(
        _("Individual End Date"),
        null=True,
        blank=True,
        help_text=_("Tenant's specific end date if different from lease.end_date"),
    )
    # --- Invite / passwordless-claim fields ---
    invited_email = models.EmailField(
        _("Invited Email"),
        blank=True,
        help_text=_(
            "Email the invite was sent to. Used to auto-link a TenantProfile "
            "on signup (or immediately, if an account with this email already "
            "exists) and as the pre-link identity in tenant lists and lease "
            "documents. Referenced by Meta.ordering, clean(), __str__, and "
            "the serializer's duplicate-invite guard — do not remove."
        ),
    )
    invited_name = models.CharField(
        _("Invited Name"),
        max_length=150,
        blank=True,
        default="",
        help_text=_(
            "Full legal name as entered by the landlord when inviting — used to "
            "fill the lease form (RTB-1 parties/signature blocks) before the "
            "tenant registers. Once the tenant links an account, their account "
            "name is authoritative and this becomes a fallback."
        ),
    )
    invited_phone = PhoneField(_("Invited Phone"))
    invite_token = models.UUIDField(
        _("Invite Token"), default=uuid.uuid4, editable=False, unique=True
    )
    invite_sent_at = models.DateTimeField(_("Invite Sent At"), null=True, blank=True)
    invite_accepted_at = models.DateTimeField(
        _("Invite Accepted At"), null=True, blank=True
    )
    tenant_notes = models.TextField(_("Notes specific to this tenant"), blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Lease Tenant")
        verbose_name_plural = _("Lease Tenants")
        # Note: unique_together on tenant no longer enforces uniqueness while tenant is null
        # (Postgres treats NULLs as distinct), so duplicate-invite protection is handled
        # in the serializer via invited_email + lease uniqueness instead.
        ordering = ["lease", "invited_email"]

    def __str__(self):
        who = (
            self.tenant.user.name
            if self.tenant
            else (self.invited_name or self.invited_email or "Pending invite")
        )
        return f"{who} on Lease {self.lease.lease_number or self.lease.id.hex[:6]}"

    @property
    def display_name(self) -> str:
        """
        Name to show/print for this tenant slot, in priority order:
        the linked account's own name, then the landlord-entered invited
        name, then the invited email. Use this everywhere a tenant's name
        appears on documents (RTB-1 parties/signature blocks, PDFs) so an
        invited-but-unregistered tenant prints as a person, not an email.
        """
        if self.tenant_id and getattr(self.tenant.user, "name", ""):
            return self.tenant.user.name
        return self.invited_name or self.invited_email

    def clean(self):
        super().clean()
        if not self.tenant and not self.invited_email:
            raise ValidationError(
                _("Either a linked tenant or an invited_email is required.")
            )
        if self.room and not ("ROOMMATE" in self.lease.lease_type and self.lease.group):
            raise ValidationError(
                _(
                    "A specific room can only be assigned if the lease is a Roommate type linked to a Property Group."
                )
            )
        if self.room and self.lease.group and self.room.group != self.lease.group:
            raise ValidationError(
                _(
                    f"The assigned room '{self.room.name}' does not belong to the lease's group '{self.lease.group.name}'."
                )
            )
        if self.cleaning_deposit != 0 and not ("ROOMMATE" in self.lease.lease_type):
            raise ValidationError(
                _(
                    "Individual cleaning deposits should only be set for tenants on Roommate agreement types."
                )
            )
        if self.is_primary_tenant:
            other_primary_exists = (
                self.lease.lease_tenants.filter(is_primary_tenant=True)
                .exclude(pk=self.pk)
                .exists()
            )
            if other_primary_exists:
                raise ValidationError(
                    _(
                        "This lease already has a primary tenant. Only one tenant "
                        "per lease can be marked as primary — unmark the existing "
                        "one first if you want to change it."
                    )
                )
        if (
            self.individual_start_date
            and self.individual_start_date < self.lease.start_date
        ):
            raise ValidationError(
                _("Individual start date cannot be before the main lease start date.")
            )
        if (
            self.individual_end_date
            and self.lease.end_date
            and self.individual_end_date > self.lease.end_date
        ):
            raise ValidationError(
                _("Individual end date cannot be after the main lease end date.")
            )
        if (
            self.individual_start_date
            and self.individual_end_date
            and self.individual_end_date < self.individual_start_date
        ):
            raise ValidationError(
                _("Individual end date cannot be before the individual start date.")
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def sign(self):
        """Mark this tenant slot as signed and try to activate the parent lease."""
        if self.declined:
            raise ValidationError(
                _("Cannot sign a lease slot that was already declined.")
            )
        self.has_signed = True
        self.signed_date = timezone.now()
        if not self.invite_accepted_at:
            self.invite_accepted_at = timezone.now()
        self.save(
            update_fields=[
                "has_signed",
                "signed_date",
                "invite_accepted_at",
                "updated_at",
            ]
        )
        LeaseInviteEvent.objects.create(
            lease_tenant=self,
            kind=LeaseInviteEvent.Kind.SIGNED,
            actor=self.tenant.user if self.tenant_id else None,
        )
        self.lease.check_and_activate()

    def decline(self, reason=""):
        """Tenant declines to sign. Does not touch the lease's own status —
        the landlord sees this on the lease detail view and can remove/replace
        the tenant slot or terminate the lease as appropriate."""
        if self.has_signed:
            raise ValidationError(
                _("Cannot decline a lease slot that was already signed.")
            )
        self.declined = True
        self.declined_at = timezone.now()
        self.decline_reason = reason
        self.save(
            update_fields=["declined", "declined_at", "decline_reason", "updated_at"]
        )
        LeaseInviteEvent.objects.create(
            lease_tenant=self,
            kind=LeaseInviteEvent.Kind.DECLINED,
            actor=self.tenant.user if self.tenant_id else None,
        )

    def attach_tenant_profile(self, tenant_profile):
        """Called once an invited email matches/creates a TenantProfile."""
        self.tenant = tenant_profile
        self.invite_accepted_at = timezone.now()
        self.save(update_fields=["tenant", "invite_accepted_at", "updated_at"])
        LeaseInviteEvent.objects.create(
            lease_tenant=self,
            kind=LeaseInviteEvent.Kind.ACCOUNT_LINKED,
            actor=tenant_profile.user,
            metadata={"source": "model_method"},
        )

    def get_invite_url(self, frontend_base_url):
        """
        Builds the account-setup link for this pending invite. Returns None
        once the slot is linked to a real account — per the "link is live
        until it's used to set a password" rule, there's nothing left to
        retrieve at that point; the person should just log in normally.
        """
        if self.tenant_id is not None:
            return None
        base = frontend_base_url.rstrip("/")
        return f"{base}/invite/{self.id}?token={self.invite_token}"


class LeaseInviteEvent(models.Model):
    """Append-only evidence for the invite/account/signature lifecycle."""

    class Kind(models.TextChoices):
        SENT = "SENT", _("Invite sent")
        LINK_OPENED = "LINK_OPENED", _("Invite link opened")
        # Authenticated tenant opened the agreement JSON or PDF (after linking).
        LEASE_VIEWED = "LEASE_VIEWED", _("Lease agreement viewed")
        ACCOUNT_LINKED = "ACCOUNT_LINKED", _("Account linked")
        SIGNED = "SIGNED", _("Lease signed")
        DECLINED = "DECLINED", _("Lease declined")
        RESENT = "RESENT", _("Invite resent")
        # A lease term changed AFTER this person signed. Recorded against
        # them, not sent to them: the landlord keeps control of the document
        # and decides who to tell, but the record of what changed and when is
        # append-only, because "they signed a different lease" is exactly the
        # claim this evidence has to answer.
        TERMS_AMENDED = "TERMS_AMENDED", _("Terms amended after signing")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease_tenant = models.ForeignKey(
        LeaseTenant,
        on_delete=models.CASCADE,
        related_name="invite_events",
    )
    kind = models.CharField(max_length=30, choices=Kind.choices, db_index=True)
    actor = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lease_invite_events",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["lease_tenant", "kind", "-created_at"],
                name="lease_invite_event_kind_idx",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(_("Lease invite events are immutable."))
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Lease invite events are immutable."))


class LeaseLandlordSignatory(models.Model):
    """A co-landlord who is a signing party on ONE lease.

    The lease's OWNER still signs via Lease.landlord_signed; each additional
    co-landlord gets a row here and signs it. The lease only activates once the
    owner AND every signatory (AND at least one tenant) have signed
    (see Lease.check_and_activate). Invited by email; `member` links once they
    have (or claim) an account, mirroring LeaseTenant's invite→claim flow.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(
        Lease, on_delete=models.CASCADE, related_name="landlord_signatories"
    )
    member = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lease_landlord_signatures",
        help_text=_("Set once the invited co-landlord has an account."),
    )
    name = models.CharField(max_length=150, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    invite_token = models.UUIDField(default=uuid.uuid4, editable=False)
    has_signed = models.BooleanField(default=False)
    signed_date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["lease", "email"],
                condition=models.Q(email__gt=""),
                name="uniq_lease_signatory_email",
            ),
        ]

    def __str__(self):
        who = self.name or self.email or self.member_id or "?"
        return f"Co-landlord {who} on {self.lease_id}"

    @property
    def display_name(self):
        if self.member_id and self.member.name:
            return self.member.name
        return self.name or self.email

    def sign(self):
        """Mark this co-landlord's signature and try to activate the lease."""
        self.has_signed = True
        self.signed_date = timezone.now()
        self.save(update_fields=["has_signed", "signed_date", "updated_at"])
        self.lease.check_and_activate()


class RentAdjustment(models.Model):
    """
    Tracks any modification to a tenant's base rent — first-month proration,
    a negotiated discount, or a rent increase — as an explicit, timestamped
    record rather than mutating rent_amount directly. This keeps a paper trail
    for why a given Payment.amount_due differs from LeaseTenant.rent_amount.
    """

    class AdjustmentType(models.TextChoices):
        PRORATION = "PRORATION", _("First Month Proration")
        DISCOUNT = "DISCOUNT", _("Negotiated Discount")
        INCREASE = "INCREASE", _("Rent Increase")
        OTHER = "OTHER", _("Other")

    class CalculationMethod(models.TextChoices):
        EXACT_NIGHTLY = "EXACT_NIGHTLY", _("Exact per-night calculation")
        FLAT_AMOUNT = "FLAT_AMOUNT", _("Flat amount")
        PERCENTAGE = "PERCENTAGE", _("Percentage")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease_tenant = models.ForeignKey(
        LeaseTenant, on_delete=models.CASCADE, related_name="rent_adjustments"
    )
    adjustment_type = models.CharField(
        _("Adjustment Type"), max_length=20, choices=AdjustmentType.choices
    )
    calculation_method = models.CharField(
        _("Calculation Method"), max_length=20, choices=CalculationMethod.choices
    )
    amount = models.DecimalField(
        _("Amount"),
        max_digits=10,
        decimal_places=2,
        help_text=_(
            "Dollar amount off/on for FLAT_AMOUNT, or the % value for PERCENTAGE (ignored for EXACT_NIGHTLY, use nights fields)"
        ),
    )
    nights_charged = models.PositiveIntegerField(
        _("Nights Charged"), null=True, blank=True
    )
    nights_in_period = models.PositiveIntegerField(
        _("Nights in Billing Period"), null=True, blank=True
    )
    reason = models.TextField(_("Reason"), blank=True)
    effective_date = models.DateField(_("Effective Date"))
    end_date = models.DateField(
        _("End Date"),
        null=True,
        blank=True,
        help_text=_("Blank = ongoing until removed"),
    )
    is_recurring = models.BooleanField(
        _("Recurring"),
        default=False,
        help_text=_(
            "Applies every billing cycle vs. a one-time adjustment (e.g. first-month proration)"
        ),
    )
    created_by = models.ForeignKey(
        LandlordProfile,
        on_delete=models.PROTECT,
        related_name="rent_adjustments_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Rent Adjustment")
        verbose_name_plural = _("Rent Adjustments")
        ordering = ["-effective_date", "-created_at"]

    def __str__(self):
        return f"{self.get_adjustment_type_display()} ({self.amount}) for {self.lease_tenant} from {self.effective_date}"

    def clean(self):
        super().clean()
        if self.calculation_method == self.CalculationMethod.EXACT_NIGHTLY:
            if not self.nights_charged or not self.nights_in_period:
                raise ValidationError(
                    _(
                        "EXACT_NIGHTLY adjustments require both nights_charged and nights_in_period."
                    )
                )
            if self.nights_charged > self.nights_in_period:
                raise ValidationError(
                    _("nights_charged cannot exceed nights_in_period.")
                )
        if self.calculation_method == self.CalculationMethod.PERCENTAGE and (
            self.amount < 0 or self.amount > 100
        ):
            raise ValidationError(
                _("Percentage adjustments must be between 0 and 100.")
            )
        if self.end_date and self.end_date < self.effective_date:
            raise ValidationError(_("end_date cannot be before effective_date."))

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def get_adjusted_amount(self, base_rent):
        """
        Returns the rent amount AFTER applying this adjustment to a base rent value.
        Positive adjustment_type=INCREASE adds; PRORATION/DISCOUNT reduce.
        """
        base_rent = Decimal(base_rent)
        if self.calculation_method == self.CalculationMethod.EXACT_NIGHTLY:
            per_night = base_rent / Decimal(self.nights_in_period)
            return (per_night * Decimal(self.nights_charged)).quantize(Decimal("0.01"))
        if self.calculation_method == self.CalculationMethod.FLAT_AMOUNT:
            delta = Decimal(self.amount)
        elif self.calculation_method == self.CalculationMethod.PERCENTAGE:
            delta = base_rent * (Decimal(self.amount) / Decimal("100"))
        else:
            delta = Decimal("0.00")
        if self.adjustment_type == self.AdjustmentType.INCREASE:
            return (base_rent + delta).quantize(Decimal("0.01"))
        # PRORATION / DISCOUNT / OTHER all reduce by default
        return max(base_rent - delta, Decimal("0.00")).quantize(Decimal("0.01"))

    @classmethod
    def create_proration(cls, lease_tenant, move_in_date, period_end_date, created_by):
        """
        Convenience constructor for the classic 'moved in mid-month' case.
        nights_in_period = total nights in that first billing period (e.g. days in the month).
        nights_charged = nights actually occupied from move_in_date through period_end_date.
        """
        nights_in_period = (period_end_date - period_end_date.replace(day=1)).days + 1
        nights_charged = (period_end_date - move_in_date).days + 1
        return cls.objects.create(
            lease_tenant=lease_tenant,
            adjustment_type=cls.AdjustmentType.PRORATION,
            calculation_method=cls.CalculationMethod.EXACT_NIGHTLY,
            amount=Decimal("0.00"),
            nights_charged=nights_charged,
            nights_in_period=nights_in_period,
            reason=f"Prorated for move-in on {move_in_date}",
            effective_date=move_in_date,
            end_date=period_end_date,
            is_recurring=False,
            created_by=created_by,
        )


class LeaseDocument(models.Model):
    """Stores additional documents related to a specific lease."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(
        Lease, on_delete=models.CASCADE, related_name="additional_documents"
    )
    title = models.CharField(_("Document Title"), max_length=255)
    document = models.FileField(
        _("Document File"), upload_to="lease_documents/%Y/%m/additional/"
    )
    description = models.TextField(_("Description"), blank=True)
    is_signed = models.BooleanField(
        _("Is Signed"),
        default=False,
        help_text=_("Indicates if this specific document requires/has signatures"),
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Lease Document")
        verbose_name_plural = _("Lease Documents")
        ordering = ["-uploaded_at"]

    def __str__(self):
        return (
            f"{self.title} for Lease {self.lease.lease_number or self.lease.id.hex[:6]}"
        )


class Payment(models.Model):
    class PaymentType(models.TextChoices):
        RENT = "RENT", _("Rent Payment")
        SECURITY_DEPOSIT = "SECURITY_DEPOSIT", _("Security Deposit")
        PET_DEPOSIT = "PET_DEPOSIT", _("Pet Deposit")
        CLEANING_DEPOSIT = "CLEANING_DEPOSIT", _("Cleaning Deposit")
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
    tenant = models.ForeignKey(
        TenantProfile, on_delete=models.PROTECT, related_name="payments"
    )
    payment_type = models.CharField(
        _("Payment Type"), max_length=20, choices=PaymentType.choices
    )
    amount_due = models.DecimalField(_("Amount Due"), max_digits=10, decimal_places=2)
    amount_paid = models.DecimalField(
        _("Amount Paid"), max_digits=10, decimal_places=2, null=True, blank=True
    )
    due_date = models.DateField(_("Due Date"))
    payment_date = models.DateField(
        _("Payment Date"),
        null=True,
        blank=True,
        help_text=_("Date payment was completed"),
    )
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.SCHEDULED,
    )
    payment_method = models.CharField(
        _("Payment Method"),
        max_length=20,
        choices=PaymentMethod.choices,
        null=True,
        blank=True,
        default=PaymentMethod.NA,
    )
    reference_number = models.CharField(
        _("Reference/Transaction ID"),
        max_length=100,
        blank=True,
        help_text=_(
            "Optional reference for tracking (e.g., cheque number, transaction ID)"
        ),
    )
    # Links this payment back to the adjustment(s) that produced amount_due, for auditability
    rent_adjustment = models.ForeignKey(
        RentAdjustment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
    )
    notes = models.TextField(_("Notes"), blank=True)
    receipt_file = models.FileField(
        _("Receipt File"), upload_to="payment_receipts/%Y/%m/", null=True, blank=True
    )
    utility_type = models.CharField(
        _("Utility Type"),
        max_length=50,
        blank=True,
        help_text=_(
            "If this is a utility payment, specify which utility (e.g., electricity, water)"
        ),
    )
    utility_provider = models.CharField(
        _("Utility Provider"),
        max_length=100,
        blank=True,
        help_text=_("Provider name for utility payments"),
    )
    utility_period_start = models.DateField(
        _("Utility Period Start"), null=True, blank=True
    )
    utility_period_end = models.DateField(
        _("Utility Period End"), null=True, blank=True
    )
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
        if self.payment_type == self.PaymentType.UTILITY:
            if not self.utility_type:
                raise ValidationError(
                    _("Utility type must be specified for utility payments.")
                )
            if not self.utility_provider:
                raise ValidationError(
                    _("Utility provider must be specified for utility payments.")
                )

    def save(self, *args, **kwargs):
        today = timezone.now().date()
        if self.amount_paid is not None:
            if self.status not in [
                Payment.PaymentStatus.REFUNDED,
                Payment.PaymentStatus.CANCELLED,
            ]:
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
    payment = models.ForeignKey(
        Payment, on_delete=models.CASCADE, related_name="reminders"
    )
    reminder_date = models.DateField(
        _("Reminder Date"), help_text=_("Date the reminder should be sent")
    )
    message_template = models.TextField(
        _("Message Template"),
        blank=True,
        help_text=_("Optional custom message, otherwise default used"),
    )
    is_sent = models.BooleanField(_("Is Sent"), default=False)
    sent_date = models.DateTimeField(_("Date Sent"), null=True, blank=True)
    send_method = models.CharField(
        _("Send Method"),
        max_length=10,
        default="EMAIL",
        choices=[("EMAIL", "Email"), ("SMS", "SMS"), ("APP", "In-App")],
    )
    error_message = models.TextField(
        _("Error Message"), blank=True, help_text=_("Records any error during sending")
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Payment Reminder")
        verbose_name_plural = _("Payment Reminders")
        ordering = ["reminder_date"]

    def __str__(self):
        status = "Sent" if self.is_sent else "Pending"
        return (
            f"{status} reminder for Payment {self.payment_id} on {self.reminder_date}"
        )
