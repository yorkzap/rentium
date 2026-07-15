"""
Lightweight landlord <-> tenant messaging.

A Conversation is a thread between one landlord and one tenant (optionally
pinned to a lease). Messages are append-only in spirit (no edit UI); posting a
message publishes a "message.created" event so the notification fan-out pings
the other participant — same outbox everything else uses.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rentium.leases.models import Lease
from rentium.users.models import LandlordProfile
from rentium.users.models import TenantProfile


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="conversations"
    )
    tenant = models.ForeignKey(
        TenantProfile, on_delete=models.CASCADE, related_name="conversations"
    )
    lease = models.ForeignKey(
        Lease,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
    )
    subject = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["landlord", "tenant", "lease"], name="uniq_conversation_scope"
            ),
        ]

    def __str__(self):
        return f"Conversation {self.landlord_id} <-> {self.tenant_id}"

    def touch(self):
        self.save(update_fields=["updated_at"])


class Message(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_messages",
    )
    body = models.TextField()
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Message {self.pk} in {self.conversation_id}"
