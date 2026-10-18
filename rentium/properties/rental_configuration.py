"""A dated offering arrangement; physical units and listings keep their IDs."""

import uuid

from django.db import models


class RentalConfiguration(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.ForeignKey(
        "properties.PropertyUnit",
        on_delete=models.PROTECT,
        related_name="rental_configurations",
    )
    mode = models.CharField(
        max_length=20,
        choices=[("WHOLE_UNIT", "Whole unit"), ("BY_ROOM", "Rooms / room bundles")],
    )
    effective_from = models.DateField()
    status = models.CharField(
        max_length=16,
        default="SCHEDULED",
        choices=[
            ("SCHEDULED", "Scheduled"),
            ("ACTIVE", "Current"),
            ("SUPERSEDED", "Previous"),
            ("CANCELLED", "Cancelled"),
            ("BLOCKED", "Needs attention"),
        ],
    )
    offering_ids = models.JSONField(default=list)
    offerings = models.ManyToManyField(
        "properties.Property", related_name="rental_configurations"
    )
    snapshot = models.JSONField(default=dict)
    approved_by = models.ForeignKey("users.User", null=True, on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(auto_now_add=True)
    receipt_key = models.CharField(max_length=64, unique=True)
    blocked_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["effective_from", "approved_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["unit", "effective_from"],
                condition=models.Q(status__in=["SCHEDULED", "ACTIVE", "BLOCKED"]),
                name="one_configuration_per_unit_date",
            )
        ]
