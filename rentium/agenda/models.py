"""
Custom calendar entries. Most of what shows on the calendar is DERIVED from
data the app already has (lease dates, rent due dates, scheduled work orders) —
see agenda/api/views.py — so we only store the landlord's own manual entries
here (inspections, reminders, notes).
"""

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _

from rentium.leases.models import Lease
from rentium.properties.models import Property
from rentium.users.models import LandlordProfile


class AgendaEvent(models.Model):
    class Kind(models.TextChoices):
        CUSTOM = "CUSTOM", _("Custom")
        INSPECTION = "INSPECTION", _("Inspection")
        REMINDER = "REMINDER", _("Reminder")
        MOVE = "MOVE", _("Move-in / Move-out")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(LandlordProfile, on_delete=models.CASCADE, related_name="agenda_events")
    title = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.CUSTOM)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(null=True, blank=True)
    property = models.ForeignKey(Property, on_delete=models.SET_NULL, null=True, blank=True, related_name="agenda_events")
    lease = models.ForeignKey(Lease, on_delete=models.SET_NULL, null=True, blank=True, related_name="agenda_events")
    # RAMA archives manual events instead of deleting them.  The UI's existing
    # DELETE remains available, while chat gets a reversible cancel/restore path.
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_date"]

    def __str__(self):
        return f"{self.title} ({self.start_date})"
