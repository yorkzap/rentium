"""
Event-driven pub/sub (transactional outbox) + user-facing Notifications.

- DomainEvent is the append-only outbox (business code publishes to it).
- Notification is the per-user feed the frontend reads. A single fan-out
  handler (see notify.py) turns relevant DomainEvents into Notifications for
  the right recipients, so the bell icon / notifications screen just reads
  one table.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class DomainEvent(models.Model):
    """Append-only. Never updated except to mark processing outcome."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(_("Event Type"), max_length=100, db_index=True)
    property_id = models.UUIDField(null=True, blank=True, db_index=True)
    lease_id = models.UUIDField(null=True, blank=True, db_index=True)
    payload = models.JSONField(_("Payload"), default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["event_type", "created_at"])]

    def __str__(self):
        return f"{self.event_type} @ {self.created_at:%Y-%m-%d %H:%M}"

    def mark_processed(self, error: str = ""):
        self.processed_at = timezone.now()
        self.error = error
        self.save(update_fields=["processed_at", "error"])


class Notification(models.Model):
    """A message shown to one user. Derived from events, or created directly."""

    class Category(models.TextChoices):
        MAINTENANCE = "MAINTENANCE", _("Maintenance")
        PAYMENT = "PAYMENT", _("Payment")
        LEASE = "LEASE", _("Lease")
        MESSAGE = "MESSAGE", _("Message")
        SYSTEM = "SYSTEM", _("System")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    event = models.ForeignKey(
        DomainEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    category = models.CharField(
        max_length=12, choices=Category.choices, default=Category.SYSTEM
    )
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    url = models.CharField(
        max_length=300, blank=True, help_text=_("Optional in-app deep link.")
    )
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "read_at", "created_at"])]
        constraints = [
            # One notification per (event, recipient) so event replay can't duplicate.
            models.UniqueConstraint(
                fields=["event", "recipient"],
                name="uniq_notification_event_recipient",
                condition=models.Q(event__isnull=False),
            ),
        ]

    def __str__(self):
        return f"{self.title} -> {self.recipient_id}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def mark_read(self):
        if not self.read_at:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at"])
