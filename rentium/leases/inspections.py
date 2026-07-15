# inspections.py
"""
Condition Inspection Reports (BC RTB-27 pattern) — lease-lifecycle documents.

Lives inside the leases app deliberately (same pattern as occupancy.py: a
separate module whose models are imported by leases/models.py). An
inspection has no identity without a lease — it's keyed by
lease/lease_tenant, bounded by possession and move-out dates, and its
endgame (deposit deductions) belongs to the lease. That's a lease concern,
not a new bounded context.

Modeling philosophy — ONE document per tenancy, exactly like the paper
RTB-27: a single ConditionInspection with two "passes" (move-in / move-out
columns) over the same item rows. The move-out column is only meaningful
NEXT TO the move-in column; keeping them on one record makes the diff, the
tenant's signature story, and the future PDF export all trivial.

Immutability (softer form of the ledger rule): once a pass is fully signed
by both parties, that pass's columns become read-only at the API layer.
Corrections are what the RTB itself prescribes — an addendum/new report —
never silent edits to a signed document.

Wire-up required in leases/models.py (mirrors the existing occupancy line):

    from .inspections import (  # noqa: E402,F401
        AreaConditionState,
        ConditionInspection,
        InspectionItem,
        InspectionKeyRow,
        InspectionTemplate,
        InspectionTemplateItem,
    )
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


# --------------------------------------------------------------- code sets
class ConditionCode(models.TextChoices):
    """RTB-27 repair-state codes (the form's ✓ / F / P / M / D / S / B)."""

    GOOD = "GOOD", _("Good (✓)")
    FAIR = "FAIR", _("Fair (F)")
    POOR = "POOR", _("Poor (P)")
    MISSING = "MISSING", _("Missing (M)")
    DAMAGED = "DAMAGED", _("Damaged (D)")
    SCRATCHED = "SCRATCHED", _("Scratched (S)")
    BROKEN = "BROKEN", _("Broken (B)")


class CleanlinessCode(models.TextChoices):
    """RTB-27 cleanliness codes — the form allows ONE of these alongside
    one repair-state code per line."""

    DIRTY = "DIRTY", _("Dirty (DT)")
    STAINED = "STAINED", _("Stained (ST)")


# Codes that auto-flag an item as a maintenance suggestion candidate.
ATTENTION_CODES = {
    ConditionCode.POOR,
    ConditionCode.MISSING,
    ConditionCode.DAMAGED,
    ConditionCode.BROKEN,
}


class InspectionPass(models.TextChoices):
    MOVE_IN = "MOVE_IN", _("Move-in")
    MOVE_OUT = "MOVE_OUT", _("Move-out")


# ---------------------------------------------------------------- templates
class InspectionTemplate(models.Model):
    """
    Seeded, versioned item catalogues per province. Creating an inspection
    COPIES rows out of the template (see inspection_services.build_inspection),
    so later template edits never mutate historical, signed reports — the
    same append-only philosophy as the ledger.

    BC ships first (RTB-27). Saskatchewan later is a new seed run, zero code.
    """

    class Province(models.TextChoices):
        BC = "BC", _("British Columbia")
        SK = "SK", _("Saskatchewan")
        GENERIC = "GENERIC", _("Generic")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(_("Name"), max_length=150)
    province = models.CharField(
        _("Province"), max_length=10, choices=Province.choices, db_index=True
    )
    version = models.PositiveIntegerField(_("Version"), default=1)
    is_active = models.BooleanField(_("Active"), default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Inspection Template")
        verbose_name_plural = _("Inspection Templates")
        ordering = ["province", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["province", "version"], name="uniq_template_province_version"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.province} v{self.version})"


class InspectionTemplateItem(models.Model):
    template = models.ForeignKey(
        InspectionTemplate, on_delete=models.CASCADE, related_name="items"
    )
    section = models.CharField(
        _("Section"), max_length=60, help_text=_("RTB-27 room/section, e.g. 'Kitchen'.")
    )
    label = models.CharField(
        _("Item"), max_length=120, help_text=_("Line item, e.g. 'Walls and Trim'.")
    )
    sort_order = models.PositiveIntegerField(_("Sort Order"), default=0)

    class Meta:
        verbose_name = _("Inspection Template Item")
        verbose_name_plural = _("Inspection Template Items")
        ordering = ["template", "sort_order"]

    def __str__(self):
        return f"{self.section} — {self.label}"


# ----------------------------------------------------- persistent condition
class AreaConditionState(models.Model):
    """
    The CURRENT condition of a physical Area — the state that survives
    tenancies. Inspections prefill from it and write back to it on pass
    completion, so damage recorded at tenant A's move-out is exactly what
    tenant B's move-in inspection shows by default (until repaired or
    manually edited in the property's Condition section).

    Deliberately a OneToOne satellite here rather than fields on
    properties.Area: the properties app stays ignorant of inspections, and
    an Area with no row simply means "GOOD, never assessed" — nothing is
    required at property-creation time.

    NOTE: this targets the NEW Area model (properties/areas.py — the one
    maintenance work orders link to), not the legacy PropertyArea in
    properties/models.py. Inventory items need no satellite: InventoryItem
    and SharedInventoryItem already carry a `condition` field, which the
    write-back updates directly.
    """

    area = models.OneToOneField(
        "properties.Area", on_delete=models.CASCADE, related_name="condition_state"
    )
    condition = models.CharField(
        _("Condition"),
        max_length=10,
        choices=ConditionCode.choices,
        default=ConditionCode.GOOD,
    )
    note = models.CharField(_("Note"), max_length=255, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    source_inspection = models.ForeignKey(
        "leases.ConditionInspection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=_("The inspection whose completion last wrote this, if any."),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Area Condition")
        verbose_name_plural = _("Area Conditions")

    def __str__(self):
        return f"{self.area} — {self.get_condition_display()}"


# ------------------------------------------------------------- inspection
class ConditionInspection(models.Model):
    """
    One RTB-27-shaped document per tenancy.

    Scope: complete-unit lease -> one inspection for the lease
    (lease_tenant is NULL, any linked tenant may sign); room/group lease ->
    one inspection PER LeaseTenant covering their room + the shared areas
    their room touches (resolved via areas_for_tenant_room, same machinery
    maintenance uses).
    """

    class Status(models.TextChoices):
        MOVE_IN_IN_PROGRESS = "MOVE_IN_IN_PROGRESS", _("Move-in — In Progress")
        MOVE_IN_SIGNED = "MOVE_IN_SIGNED", _("Move-in Signed")
        MOVE_OUT_IN_PROGRESS = "MOVE_OUT_IN_PROGRESS", _("Move-out — In Progress")
        COMPLETED = "COMPLETED", _("Completed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(
        "leases.Lease", on_delete=models.PROTECT, related_name="inspections"
    )
    lease_tenant = models.ForeignKey(
        "leases.LeaseTenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inspections",
        help_text=_("Set for room/group leases; NULL for complete-unit leases."),
    )
    template = models.ForeignKey(
        InspectionTemplate,
        on_delete=models.PROTECT,
        related_name="inspections",
        help_text=_("Provenance: which template version seeded this document."),
    )
    status = models.CharField(
        _("Status"),
        max_length=25,
        choices=Status.choices,
        default=Status.MOVE_IN_IN_PROGRESS,
        db_index=True,
    )

    # --- RTB-27 header boxes E–I -----------------------------------------
    possession_date = models.DateField(_("Possession Date"), null=True, blank=True)
    move_in_inspection_date = models.DateField(
        _("Move-in Inspection Date"), null=True, blank=True
    )
    move_out_date = models.DateField(_("Move-out Date"), null=True, blank=True)
    move_out_inspection_date = models.DateField(
        _("Move-out Inspection Date"), null=True, blank=True
    )
    tenant_agent_move_in = models.CharField(
        _("Tenant's Agent (move-in)"), max_length=150, blank=True
    )
    tenant_agent_move_out = models.CharField(
        _("Tenant's Agent (move-out)"), max_length=150, blank=True
    )

    # --- Box X / Box Z ----------------------------------------------------
    repairs_required_at_start = models.TextField(
        _("Repairs Required at Start (Box X)"), blank=True
    )
    tenant_responsible_damage = models.TextField(
        _("Tenant-Responsible Damage (Box Z)"), blank=True
    )

    # --- Boxes Y / 1: tenant agreement (null = not answered yet) ----------
    tenant_agrees_move_in = models.BooleanField(
        _("Tenant Agrees (move-in)"), null=True, blank=True
    )
    tenant_disagreement_move_in = models.TextField(
        _("Disagreement Reason (move-in)"), blank=True
    )
    tenant_agrees_move_out = models.BooleanField(
        _("Tenant Agrees (move-out)"), null=True, blank=True
    )
    tenant_disagreement_move_out = models.TextField(
        _("Disagreement Reason (move-out)"), blank=True
    )

    # --- Signatures (click-to-sign: typed legal name + auth user + ts) ----
    landlord_signed_move_in_at = models.DateTimeField(null=True, blank=True)
    landlord_move_in_signature_name = models.CharField(max_length=150, blank=True)
    tenant_signed_move_in_at = models.DateTimeField(null=True, blank=True)
    tenant_move_in_signature_name = models.CharField(max_length=150, blank=True)
    landlord_signed_move_out_at = models.DateTimeField(null=True, blank=True)
    landlord_move_out_signature_name = models.CharField(max_length=150, blank=True)
    tenant_signed_move_out_at = models.DateTimeField(null=True, blank=True)
    tenant_move_out_signature_name = models.CharField(max_length=150, blank=True)

    # --- Box 2: tenant-consented deposit deductions (ledger wiring: ph. 2)
    deduction_security_deposit = models.DecimalField(
        _("Agreed Deduction — Security Deposit"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    deduction_pet_deposit = models.DecimalField(
        _("Agreed Deduction — Pet Deposit"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    deduction_agreed_at = models.DateTimeField(null=True, blank=True)

    # --- Boxes 5 / 6 -------------------------------------------------------
    tenant_forwarding_address = models.CharField(max_length=255, blank=True)

    # --- Compliance clocks (7-day / 15-day delivery rules) -----------------
    move_in_report_delivered_at = models.DateTimeField(null=True, blank=True)
    move_out_report_delivered_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspections_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Condition Inspection")
        verbose_name_plural = _("Condition Inspections")
        ordering = ["-created_at"]
        constraints = [
            # One inspection per roommate tenancy...
            models.UniqueConstraint(
                fields=["lease", "lease_tenant"],
                condition=models.Q(lease_tenant__isnull=False),
                name="uniq_inspection_per_lease_tenant",
            ),
            # ...and one per complete-unit lease (Postgres treats NULLs as
            # distinct, so the constraint above alone wouldn't stop dupes).
            models.UniqueConstraint(
                fields=["lease"],
                condition=models.Q(lease_tenant__isnull=True),
                name="uniq_inspection_per_unit_lease",
            ),
        ]

    def __str__(self):
        who = self.lease_tenant or self.lease.lease_number or self.lease_id
        return f"Inspection for {who} [{self.get_status_display()}]"

    def clean(self):
        super().clean()
        if self.lease_tenant and self.lease_tenant.lease_id != self.lease_id:
            raise ValidationError(
                {"lease_tenant": _("Lease tenant does not belong to this lease.")}
            )
        if (
            self.move_in_inspection_date
            and self.possession_date
            and self.move_in_inspection_date < self.possession_date
        ):
            # Soft rule on the paper form too — inspection is ideally ON the
            # possession day; earlier makes no sense.
            raise ValidationError(
                {
                    "move_in_inspection_date": _(
                        "Move-in inspection can't be before the possession date."
                    )
                }
            )

    # ------------------------------------------------------ derived state
    @property
    def move_in_fully_signed(self) -> bool:
        return bool(self.landlord_signed_move_in_at and self.tenant_signed_move_in_at)

    @property
    def move_out_fully_signed(self) -> bool:
        return bool(
            self.landlord_signed_move_out_at and self.tenant_signed_move_out_at
        )

    @property
    def disputed_move_in(self) -> bool:
        return self.tenant_agrees_move_in is False

    @property
    def disputed_move_out(self) -> bool:
        return self.tenant_agrees_move_out is False

    def pass_is_locked(self, pass_name: str) -> bool:
        """A fully-signed pass's columns are read-only (RTB answer to
        corrections is an addendum, never silent edits)."""
        if pass_name == InspectionPass.MOVE_IN:
            return self.move_in_fully_signed
        return self.move_out_fully_signed


class InspectionItem(models.Model):
    """
    One line of the report — a template row, an injected inventory row, or a
    landlord-added custom row (the paper form's blank lines). Carries BOTH
    columns (beginning / end of tenancy), mirroring RTB-27 exactly.
    """

    class SuggestionStatus(models.TextChoices):
        NONE = "NONE", _("No suggestion")
        PENDING = "PENDING", _("Pending landlord review")
        APPROVED = "APPROVED", _("Approved — work order created")
        DISMISSED = "DISMISSED", _("Dismissed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inspection = models.ForeignKey(
        ConditionInspection, on_delete=models.CASCADE, related_name="items"
    )
    section = models.CharField(_("Section"), max_length=60)
    label = models.CharField(_("Item"), max_length=200)
    sort_order = models.PositiveIntegerField(default=0)
    is_custom = models.BooleanField(
        default=False, help_text=_("Landlord-added row (the form's blank lines).")
    )

    # Physical-world links (all optional; the paper form needs none of them,
    # we just prefer them so prefill/write-back/suggestions can work).
    area = models.ForeignKey(
        "properties.Area",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspection_items",
    )
    inventory_item = models.ForeignKey(
        "properties.InventoryItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspection_items",
    )
    shared_inventory_item = models.ForeignKey(
        "properties.SharedInventoryItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspection_items",
    )

    # Beginning of tenancy
    move_in_condition_code = models.CharField(
        max_length=10, choices=ConditionCode.choices, blank=True, default=""
    )
    move_in_cleanliness_code = models.CharField(
        max_length=10, choices=CleanlinessCode.choices, blank=True, default=""
    )
    move_in_comment = models.CharField(max_length=255, blank=True)

    # End of tenancy
    move_out_condition_code = models.CharField(
        max_length=10, choices=ConditionCode.choices, blank=True, default=""
    )
    move_out_cleanliness_code = models.CharField(
        max_length=10, choices=CleanlinessCode.choices, blank=True, default=""
    )
    move_out_comment = models.CharField(max_length=255, blank=True)

    # Maintenance-suggestion pipeline: flagged items surface on the landlord
    # dashboard after pass completion; Approve creates a WorkOrder (linked
    # below), Dismiss records the condition and moves on. Work orders are
    # NEVER auto-created — the FSM's "never deleted" rule forbids
    # speculative jobs.
    needs_attention = models.BooleanField(default=False)
    suggestion_status = models.CharField(
        max_length=10, choices=SuggestionStatus.choices, default=SuggestionStatus.NONE
    )
    work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspection_items",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Inspection Item")
        verbose_name_plural = _("Inspection Items")
        ordering = ["inspection", "sort_order", "created_at"]

    def __str__(self):
        return f"{self.section} — {self.label}"

    def latest_code(self) -> str:
        """The most recent recorded repair-state code (end column wins)."""
        return self.move_out_condition_code or self.move_in_condition_code or ""


class InspectionKeyRow(models.Model):
    """Box W — keys/controls issued at start, returned at end."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inspection = models.ForeignKey(
        ConditionInspection, on_delete=models.CASCADE, related_name="key_rows"
    )
    key_type = models.CharField(_("Type of key or control"), max_length=120)
    issued_count = models.PositiveIntegerField(_("# Issued at start"), default=0)
    returned_count = models.PositiveIntegerField(
        _("# Returned at end"), null=True, blank=True
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = _("Inspection Key Row")
        verbose_name_plural = _("Inspection Key Rows")
        ordering = ["inspection", "sort_order"]

    def __str__(self):
        return f"{self.key_type} ({self.issued_count} issued)"