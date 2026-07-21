"""
Communication channels — where RAMA reaches the landlord outside the app.

One abstraction for every channel: Telegram today, email/WhatsApp later.
Adding a channel = a transport module + rows here; the event bridge and the
General's chat surface don't change.
"""

import secrets

from django.db import models
from django.utils import timezone

LINK_CODE_TTL_SECONDS = 10 * 60


class ChannelAccount(models.Model):
    """One subject (a landlord OR a tenant) ↔ one external channel address
    (e.g. a Telegram chat). Exactly one of landlord/tenant is set — the DB
    enforces it. Prospective tenants have no account and are reached by email +
    their tracking page, so they never appear here."""

    class ChannelType(models.TextChoices):
        TELEGRAM = "TELEGRAM", "Telegram"
        EMAIL = "EMAIL", "Email"
        WHATSAPP = "WHATSAPP", "WhatsApp"

    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="channel_accounts",
        null=True,
        blank=True,
    )
    tenant = models.ForeignKey(
        "users.TenantProfile",
        on_delete=models.CASCADE,
        related_name="channel_accounts",
        null=True,
        blank=True,
    )
    channel_type = models.CharField(
        max_length=20, choices=ChannelType.choices, default=ChannelType.TELEGRAM
    )
    # Telegram chat_id / email address. Filled on verification for Telegram.
    address = models.CharField(max_length=200, blank=True, default="")
    display_name = models.CharField(max_length=120, blank=True, default="")
    verified = models.BooleanField(default=False)
    link_code = models.CharField(max_length=12, blank=True, default="")
    link_code_expires = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    # {"categories": ["MAINTENANCE", ...] (empty = all), "quiet_hours": [22, 7],
    #  "briefing": false}
    prefs = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["channel_type", "address"],
                condition=~models.Q(address=""),
                name="comms_channel_address_unique",
            ),
            # Exactly one subject: a landlord XOR a tenant, never both/neither.
            models.CheckConstraint(
                check=(
                    models.Q(landlord__isnull=False, tenant__isnull=True)
                    | models.Q(landlord__isnull=True, tenant__isnull=False)
                ),
                name="comms_channel_one_subject",
            ),
        ]

    def __str__(self):
        state = "verified" if self.verified else "pending"
        return f"{self.channel_type} {self.address or self.link_code} ({state})"

    # ----------------------------------------------------------- subject
    @property
    def subject(self):
        """The owning profile — a LandlordProfile or a TenantProfile."""
        return self.landlord or self.tenant

    @property
    def subject_user(self):
        subject = self.subject
        return getattr(subject, "user", None) if subject else None

    @property
    def is_tenant(self) -> bool:
        return self.tenant_id is not None

    @staticmethod
    def _subject_kwargs(subject) -> dict:
        """Route a subject to the right FK. Duck-typed on the reverse relation
        name so we don't import both profile models here."""
        from rentium.users.models import TenantProfile

        return {"tenant": subject} if isinstance(subject, TenantProfile) else {
            "landlord": subject
        }

    # ------------------------------------------------------------- linking
    @classmethod
    def mint_link_code(cls, subject, channel_type: str) -> "ChannelAccount":
        """A fresh short-lived code the subject (landlord or tenant) sends to
        the bot to bind their chat."""
        code = secrets.token_hex(3).upper()  # 6 hex chars, human-typable
        account, _ = cls.objects.update_or_create(
            **cls._subject_kwargs(subject),
            channel_type=channel_type,
            verified=False,
            address="",
            defaults={
                "link_code": code,
                "link_code_expires": timezone.now()
                + timezone.timedelta(seconds=LINK_CODE_TTL_SECONDS),
            },
        )
        return account

    @classmethod
    def redeem_link_code(cls, code: str, *, channel_type: str, address: str,
                         display_name: str = "") -> "ChannelAccount | None":
        """Bind an external address to the landlord who minted `code`."""
        code = (code or "").strip().upper()
        if not code:
            return None
        account = cls.objects.filter(
            channel_type=channel_type,
            link_code=code,
            verified=False,
            link_code_expires__gt=timezone.now(),
        ).first()
        if account is None:
            return None
        # An address can only serve one landlord; rebinding moves it.
        cls.objects.filter(channel_type=channel_type, address=address).delete()
        account.address = address
        account.display_name = display_name[:120]
        account.verified = True
        account.link_code = ""
        account.link_code_expires = None
        account.save()
        return account

    def wants_category(self, category: str) -> bool:
        cats = (self.prefs or {}).get("categories") or []
        return not cats or category in cats
