"""
Maintenance work orders.

Named WorkOrder (not "request") because not all work is tenant-requested —
landlords file preventive/routine jobs too; `origin` records who initiated.

Visibility is area-aware: tenants see work orders on their room, on common
areas they share, and on areas exclusive to their room (see the viewset).

Lifecycle is a strict FSM (core.fsm) — a job can't jump NEW -> COMPLETED —
and every legal transition publishes a domain event.

SLA: BC's RTA (s.33) singles out *emergency repairs* (heat, major leaks,
water, electricity, locks) as requiring prompt action but prescribes no hour
counts, so the deadlines below are configurable house-policy defaults, with
emergency-priority jobs flagged as RTA-covered. `sla_due_at` is set at
creation; the clock stops when the job is first actioned (SCHEDULED or
IN_PROGRESS); a beat task publishes maintenance.sla_breached.
"""

import builtins
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rentium.core.fsm import transition
from rentium.core.phone import PhoneField
from rentium.events.registry import publish
from rentium.leases.models import Lease
from rentium.properties.models import PropertyArea
from rentium.properties.models import Property

# House-policy response deadlines by priority (hours). EMERGENCY categories
# are the RTA s.33 emergency-repair territory — surfaced in the UI.
DEFAULT_SLA_HOURS = {
    "EMERGENCY": 24,
    "HIGH": 72,
    "MEDIUM": 24 * 7,
    "LOW": 24 * 14,
}
RTA_EMERGENCY_CATEGORIES = {"PLUMBING", "HEATING_COOLING", "ELECTRICAL", "SAFETY"}


class WorkOrder(models.Model):
    class Category(models.TextChoices):
        PLUMBING = "PLUMBING", _("Plumbing")
        ELECTRICAL = "ELECTRICAL", _("Electrical")
        HEATING_COOLING = "HEATING_COOLING", _("Heating / Cooling")
        APPLIANCE = "APPLIANCE", _("Appliance")
        STRUCTURAL = "STRUCTURAL", _("Structural / Doors / Windows")
        PEST = "PEST", _("Pest Control")
        SAFETY = "SAFETY", _("Safety (locks, detectors)")
        OTHER = "OTHER", _("Other")

    class Priority(models.TextChoices):
        LOW = "LOW", _("Low")
        MEDIUM = "MEDIUM", _("Medium")
        HIGH = "HIGH", _("High")
        EMERGENCY = "EMERGENCY", _("Emergency")

    class Status(models.TextChoices):
        NEW = "NEW", _("New")
        SCHEDULED = "SCHEDULED", _("Scheduled")
        IN_PROGRESS = "IN_PROGRESS", _("In Progress")
        COMPLETED = "COMPLETED", _("Completed")
        CANCELLED = "CANCELLED", _("Cancelled")

    class Origin(models.TextChoices):
        TENANT = "TENANT", _("Tenant Reported")
        LANDLORD = "LANDLORD", _("Landlord Initiated")
        ROUTINE = "ROUTINE", _("Routine / Preventive")

    # Legal lifecycle. COMPLETED/CANCELLED are terminal (reopen = new order).
    TRANSITIONS = {
        Status.NEW: {Status.SCHEDULED, Status.IN_PROGRESS, Status.CANCELLED},
        Status.SCHEDULED: {
            Status.IN_PROGRESS,
            Status.COMPLETED,
            Status.CANCELLED,
            Status.NEW,
        },
        Status.IN_PROGRESS: {Status.COMPLETED, Status.CANCELLED},
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
    }
    ACTIONED_STATUSES = {Status.SCHEDULED, Status.IN_PROGRESS, Status.COMPLETED}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name="work_orders"
    )
    area = models.ForeignKey(
        PropertyArea,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_orders",
        help_text=_("Which space the issue is in. Blank = the room/unit itself."),
    )
    lease = models.ForeignKey(
        Lease,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_orders",
        help_text=_("Lease active when the issue was reported, if any."),
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="work_orders",
    )
    origin = models.CharField(
        _("Origin"), max_length=10, choices=Origin.choices, default=Origin.TENANT
    )
    title = models.CharField(_("Title"), max_length=200)
    description = models.TextField(_("Description"))
    category = models.CharField(
        _("Category"), max_length=20, choices=Category.choices, default=Category.OTHER
    )
    priority = models.CharField(
        _("Priority"), max_length=10, choices=Priority.choices, default=Priority.MEDIUM
    )
    status = models.CharField(
        _("Status"), max_length=15, choices=Status.choices, default=Status.NEW
    )

    scheduled_date = models.DateField(_("Scheduled Date"), null=True, blank=True)
    completed_date = models.DateField(_("Completed Date"), null=True, blank=True)
    cost = models.DecimalField(
        _("Actual Cost"), max_digits=10, decimal_places=2, null=True, blank=True
    )
    contractor_name = models.CharField(_("Contractor"), max_length=150, blank=True)
    contractor_phone = PhoneField(_("Contractor Phone"))

    # SLA timer
    sla_due_at = models.DateTimeField(_("SLA Deadline"), null=True, blank=True)
    first_actioned_at = models.DateTimeField(null=True, blank=True)
    sla_breach_notified = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Work Order")
        verbose_name_plural = _("Work Orders")

    def __str__(self):
        return f"[{self.get_priority_display()}] {self.title} — {self.property.name}"

    # -------------------------------------------------------------- hooks
    def save(self, *args, **kwargs):
        creating = self._state.adding
        if creating and self.sla_due_at is None:
            hours = DEFAULT_SLA_HOURS.get(self.priority, DEFAULT_SLA_HOURS["MEDIUM"])
            self.sla_due_at = timezone.now() + timedelta(hours=hours)
        super().save(*args, **kwargs)
        if creating:
            publish(
                "maintenance.created",
                {
                    "work_order_id": str(self.id),
                    "title": self.title,
                    "category": self.category,
                    "priority": self.priority,
                    "origin": self.origin,
                    "rta_emergency": self.is_rta_emergency,
                    "sla_due_at": self.sla_due_at.isoformat()
                    if self.sla_due_at
                    else None,
                },
                property_id=self.property_id,
                lease_id=self.lease_id,
            )

    def clean(self):
        super().clean()
        if (
            self.lease
            and self.lease.property_id
            and self.lease.property_id != self.property_id
        ):
            raise ValidationError(
                {"lease": _("The linked lease does not belong to this property.")}
            )
        if self.area:
            area_parent_ok = (
                (self.area.property_id and self.area.property_id == self.property_id)
                or (
                    self.area.group_id
                    and self.area.group_id == self.property.group_id
                )
                # The unit's internal layout: a whole-unit listing's kitchen is
                # an area on the unit, not on the listing.
                or (self.area.unit_id and self.area.unit_id == self.property.unit_id)
            )
            if not area_parent_ok:
                raise ValidationError(
                    {
                        "area": _(
                            "Area does not belong to this property, its unit, or "
                            "its group."
                        )
                    }
                )

    # -------------------------------------------------------------- FSM
    def transition_to(self, new_status: str, by=None):
        old, new = transition(self, "status", new_status, self.TRANSITIONS)
        now = timezone.now()
        updates = []
        if new in self.ACTIONED_STATUSES and self.first_actioned_at is None:
            self.first_actioned_at = now  # SLA clock stops
            updates.append("first_actioned_at")
        if new == self.Status.COMPLETED and not self.completed_date:
            self.completed_date = now.date()
            updates.append("completed_date")
        if updates:
            super().save(update_fields=updates + ["updated_at"])
        publish(
            "maintenance.status_changed",
            {
                "work_order_id": str(self.id),
                "from": old,
                "to": new,
                "by": str(getattr(by, "pk", "")) if by else None,
            },
            property_id=self.property_id,
            lease_id=self.lease_id,
        )
        return old, new

    # -------------------------------------------------------------- SLA
    # `property` FK field shadows the `property` builtin in this class body.
    @builtins.property
    def is_rta_emergency(self) -> bool:
        return (
            self.priority == self.Priority.EMERGENCY
            and self.category in RTA_EMERGENCY_CATEGORIES
        )

    @builtins.property
    def sla_breached(self) -> bool:
        return bool(
            self.sla_due_at
            and self.first_actioned_at is None
            and self.status == self.Status.NEW
            and timezone.now() > self.sla_due_at
        )


class WorkOrderImage(models.Model):
    work_order = models.ForeignKey(
        WorkOrder, on_delete=models.CASCADE, related_name="images"
    )
    image = models.ImageField(_("Image"), upload_to="maintenance/%Y/%m/")
    caption = models.CharField(_("Caption"), max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Image for {self.work_order_id}"


class WorkOrderComment(models.Model):
    work_order = models.ForeignKey(
        WorkOrder, on_delete=models.CASCADE, related_name="comments"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    body = models.TextField(_("Comment"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Comment by {self.author} on {self.work_order_id}"
