"""
rentium/appointments/models.py

Adds the public-showing flow used by every serious rental platform:
prospective tenants (no account) submit a viewing REQUEST from a public
page; the landlord CONFIRMS it into a scheduled appointment or declines.

New in v2:
  - Status.REQUESTED — a lead awaiting the landlord's confirmation.
  - contact_email — how we reach an unregistered requester.

Setup (if not done in the previous round):
  rentium/appointments/{__init__.py, models.py, api.py, public_views.py, apps.py}
  INSTALLED_APPS += ["rentium.appointments"]
  python manage.py makemigrations appointments && migrate
"""

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _

from rentium.core.phone import PhoneField


class Appointment(models.Model):
    class Kind(models.TextChoices):
        VIEWING = "VIEWING", _("Viewing / Showing")
        CONTRACTOR = "CONTRACTOR", _("Contractor / Maintenance Visit")
        OTHER = "OTHER", _("Other")

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", _("Requested (awaiting confirmation)")
        SCHEDULED = "SCHEDULED", _("Scheduled")
        COMPLETED = "COMPLETED", _("Completed")
        CANCELLED = "CANCELLED", _("Cancelled")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile", on_delete=models.CASCADE, related_name="appointments"
    )
    property = models.ForeignKey(
        "properties.Property", on_delete=models.CASCADE, related_name="appointments"
    )
    lease = models.ForeignKey(
        "leases.Lease",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
        help_text=_(
            "Current tenancy affected (tenants on it see the appointment — their entry notice)."
        ),
    )
    work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
        help_text=_("For contractor visits: the work order being serviced."),
    )
    kind = models.CharField(max_length=15, choices=Kind.choices, default=Kind.VIEWING)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.SCHEDULED
    )
    starts_at = models.DateTimeField(_("Starts At"))
    ends_at = models.DateTimeField(_("Ends At"), null=True, blank=True)
    contact_name = models.CharField(
        _("Who's Coming"),
        max_length=200,
        blank=True,
        help_text=_("Prospective tenant, contractor/company, etc."),
    )
    contact_email = models.EmailField(
        _("Contact Email"),
        blank=True,
        help_text=_("For public viewing requests: how to reach the requester."),
    )
    contact_phone = PhoneField(_("Contact Phone"))
    notes = models.TextField(_("Notes"), blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at"]
        verbose_name = _("Appointment")
        verbose_name_plural = _("Appointments")

    def __str__(self):
        return f"{self.get_kind_display()} at {self.property.name} — {self.starts_at:%Y-%m-%d %H:%M} [{self.status}]"

    def publish_event(self, event_type: str):
        from rentium.events.registry import publish

        publish(
            event_type,
            {
                "appointment_id": str(self.pk),
                "kind": self.kind,
                "status": self.status,
                "starts_at": self.starts_at.isoformat(),
                "contact_name": self.contact_name,
                "contact_email": self.contact_email,
                "work_order_id": str(self.work_order_id)
                if self.work_order_id
                else None,
            },
            property_id=self.property_id,
            lease_id=self.lease_id,
        )
