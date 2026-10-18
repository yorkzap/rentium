"""Durable, versioned facts about what a tenancy covers and its jurisdiction.

These are operational records, not replacements for signed agreements. A
correction appends a revision and preserves the original evidence.
"""

import uuid

from django.core.exceptions import ValidationError
from django.db import models


class ImmutableTenancyRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recorded_at = models.DateTimeField(auto_now_add=True)
    recorded_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    reason = models.TextField()

    class Meta:
        abstract = True
        ordering = ["-recorded_at", "-id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Append a correction; do not rewrite tenancy evidence."
            )
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Tenancy evidence is retained for audit.")


class LeaseSpaceScope(ImmutableTenancyRecord):
    lease = models.ForeignKey(
        "leases.Lease", on_delete=models.PROTECT, related_name="scope_versions"
    )
    unit = models.ForeignKey("properties.PropertyUnit", on_delete=models.PROTECT)
    whole_unit = models.BooleanField(default=False)
    # IDs of persistent physical PropertyArea bedrooms, not LeaseTenant assignments.
    room_ids = models.JSONField(default=list, blank=True)
    snapshot = models.JSONField(default=dict)

    def clean(self):
        from rentium.properties.models import PropertyArea

        if self.unit.landlord_id != self.lease.landlord_id:
            raise ValidationError("The unit is outside this lease's portfolio.")
        if self.whole_unit and self.room_ids:
            raise ValidationError(
                "A whole-unit scope cannot also select individual rooms."
            )
        if not self.whole_unit:
            ids = {str(value) for value in self.room_ids}
            found = {
                str(value)
                for value in PropertyArea.objects.filter(
                    unit=self.unit,
                    pk__in=ids,
                    area_type="BEDROOM",
                    is_seeded_default=False,
                ).values_list("pk", flat=True)
            }
            if not ids or found != ids:
                raise ValidationError(
                    "Select recorded bedrooms belonging to this unit."
                )


class TenancyAssessment(ImmutableTenancyRecord):
    lease = models.ForeignKey(
        "leases.Lease", on_delete=models.PROTECT, related_name="tenancy_assessments"
    )
    jurisdiction = models.CharField(max_length=16)
    decision = models.CharField(
        max_length=24,
        choices=[
            ("APPLIES", "Tenancy act applies"),
            ("EXEMPT_OWNER_SHARED", "Owner shares kitchen or bathroom"),
            ("REVIEW", "Needs review"),
        ],
    )
    rule_version = models.CharField(max_length=40, default="bc-owner-sharing-2026-10")
    facts = models.JSONField(default=dict, blank=True)

    def clean(self):
        if self.decision == "EXEMPT_OWNER_SHARED":
            if self.jurisdiction != "BC" or not (
                self.facts.get("owner_identity")
                and self.facts.get("owner_confirmed") is True
                and self.facts.get("shared_at_tenancy_start") is True
                and set(self.facts.get("facilities", [])) & {"KITCHEN", "BATHROOM"}
            ):
                raise ValidationError(
                    "Exemption needs an identified owner and actual kitchen/bathroom sharing at tenancy start."
                )
        if (
            self.decision == "APPLIES"
            and self.facts.get("shared_at_tenancy_start") is not False
        ):
            raise ValidationError(
                "Record that no kitchen/bathroom was shared with an owner, or request review."
            )


class TenancyPresence(ImmutableTenancyRecord):
    lease = models.ForeignKey(
        "leases.Lease", on_delete=models.PROTECT, related_name="presence_events"
    )
    state = models.CharField(
        max_length=12,
        choices=[
            ("PRESENT", "Possession confirmed"),
            ("DEPARTED", "Departure confirmed"),
        ],
    )
    occurred_on = models.DateField()
    receipt_key = models.CharField(max_length=64, unique=True)

    def clean(self):
        from django.utils import timezone

        if self.occurred_on and self.occurred_on > timezone.localdate():
            raise ValidationError("Confirm an actual event, not a future planned move.")
