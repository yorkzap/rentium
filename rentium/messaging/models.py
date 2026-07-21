"""
Lightweight landlord <-> tenant/prospect messaging.

A Conversation is a thread between one landlord and either a registered tenant
OR an accountless prospect (a lead who inquired about a listing). Messages are
append-only in spirit (no edit UI); posting a message publishes a
"message.created" event so the notification fan-out pings the other participant
— same outbox everything else uses.

Prospect (no account) security model: a prospect participates through a
per-conversation `access_token` (a bearer credential, the same pattern as
Appointment.public_token and LeaseTenant.invite_token). The token is delivered
ONLY to the prospect's own email address, grants access to exactly ONE
conversation, and the public serializer that backs it is PII-minimized — it
exposes the listing name, the landlord's display name and the messages, and
NEVER the full address, other tenants, financials, or any other listing. This
is why a leaked link can't expose a portfolio the way an open URL would.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rentium.leases.models import Lease
from rentium.properties.models import Property
from rentium.users.models import LandlordProfile
from rentium.users.models import TenantProfile


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="conversations"
    )
    # Exactly one participant identity is set (see the CheckConstraint below):
    # a registered `tenant`, OR an accountless prospect (`prospect_email`).
    tenant = models.ForeignKey(
        TenantProfile,
        on_delete=models.CASCADE,
        related_name="conversations",
        null=True,
        blank=True,
    )
    prospect_email = models.EmailField(_("Prospect email"), blank=True)
    prospect_name = models.CharField(_("Prospect name"), max_length=150, blank=True)
    # The listing the lead is about — context for a prospect thread that has no
    # lease yet. Kept even if the listing is later deleted.
    property = models.ForeignKey(
        Property,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lead_conversations",
    )
    # Bearer credential for an accountless prospect. Emailed to them only; grants
    # access to THIS conversation alone. Never shown to the landlord side.
    access_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
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
                fields=["landlord", "tenant", "lease"],
                condition=models.Q(tenant__isnull=False),
                name="uniq_conversation_scope",
            ),
            models.CheckConstraint(
                check=(
                    (models.Q(tenant__isnull=False) & models.Q(prospect_email=""))
                    | (models.Q(tenant__isnull=True) & ~models.Q(prospect_email=""))
                ),
                name="conversation_tenant_xor_prospect",
            ),
        ]

    def __str__(self):
        other = self.tenant_id or self.prospect_email or "?"
        return f"Conversation {self.landlord_id} <-> {other}"

    # NB: a field named `property` shadows the builtin in this class body, so
    # these are plain methods, not @property helpers.
    def is_prospect(self) -> bool:
        return self.tenant_id is None

    def other_display_name(self) -> str:
        if self.tenant_id:
            return self.tenant.user.name or self.tenant.user.email
        return self.prospect_name or self.prospect_email

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
