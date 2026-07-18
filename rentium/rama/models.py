"""
RAMA models.

- RamaPreferences: per-landlord opt-in + model choice (their RAMA, not a
  site-wide switch). Memory/audit is always scoped to this landlord.
- RamaAudit: append-only trail of messages, tool calls, and errors — also
  the per-conversation "memory" rebuilt into the next turn.
"""

import uuid

from django.db import models


class RamaPreferences(models.Model):
    """One row per landlord: enable RAMA, pick provider/model, optional BYOK key.

    Each landlord can paste their own API key (e.g. xAI Grok). If blank, the
    platform env key is used when present. Chat history never mixes across
    landlords.
    """

    class Provider(models.TextChoices):
        XAI = "xai", "xAI (Grok)"
        GEMINI = "gemini", "Google Gemini"
        MISTRAL = "mistral", "Mistral AI"
        ANTHROPIC = "anthropic", "Anthropic (Claude)"
        OPENAI = "openai", "OpenAI"

    landlord = models.OneToOneField(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_preferences",
        primary_key=True,
    )
    enabled = models.BooleanField(
        default=False,
        help_text="When off, the Ask RAMA panel is hidden for this landlord.",
    )
    provider = models.CharField(
        max_length=40,
        choices=Provider.choices,
        default=Provider.XAI,
    )
    model = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Empty → built-in default for the selected provider.",
    )
    # Bring-your-own-key. Never expose the full value in API responses —
    # only has_api_key. Blank on PATCH means "keep existing".
    api_key = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="Landlord's provider API key (BYOK). Preferred over platform env keys.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "RAMA preferences"
        verbose_name_plural = "RAMA preferences"

    def __str__(self) -> str:
        state = "on" if self.enabled else "off"
        return f"RAMA {state} for {self.landlord_id}: {self.provider}/{self.model or 'default'}"

    @classmethod
    def for_landlord(cls, landlord) -> "RamaPreferences":
        obj, _ = cls.objects.get_or_create(landlord=landlord)
        return obj


class RamaAudit(models.Model):
    class Kind(models.TextChoices):
        USER_MESSAGE = "USER_MESSAGE", "User message"
        ASSISTANT_MESSAGE = "ASSISTANT_MESSAGE", "Assistant message"
        TOOL_CALL = "TOOL_CALL", "Tool call"
        ERROR = "ERROR", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation_id = models.UUIDField(db_index=True)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_audits",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    provider = models.CharField(max_length=40, blank=True, default="")
    model = models.CharField(max_length=100, blank=True, default="")
    content = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["landlord", "conversation_id", "created_at"]),
        ]

    def __str__(self):
        return f"{self.kind} ({self.provider}/{self.model}) {self.created_at:%Y-%m-%d %H:%M}"


class RamaPendingAction(models.Model):
    """The write action a turn previewed and is waiting on the landlord to confirm.

    RAMA runs one independent agent turn per message and rebuilds "memory" from
    text only, so a weak model can't reliably re-issue the exact tool + arguments
    it previewed when the landlord says "yes" — it just re-previews (an infinite
    loop). We persist the pending tool call here so the backend can execute it
    deterministically on affirmation, no model reconstruction required.

    One row per conversation (the latest preview wins); consumed on execution or
    superseded by the next preview / a change of subject.
    """

    conversation_id = models.UUIDField(primary_key=True)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_pending_actions",
    )
    tool = models.CharField(max_length=100)
    arguments = models.JSONField(default=dict)
    # Human-readable preview echoed back so the UI/model has continuity.
    preview = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "RAMA pending action"
        verbose_name_plural = "RAMA pending actions"

    def __str__(self):
        return f"pending {self.tool} for {self.landlord_id} ({self.conversation_id})"
