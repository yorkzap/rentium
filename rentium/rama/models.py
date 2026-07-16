"""
RAMA's audit spine. One row per event — user message, tool call, assistant
reply, error — so every answer is attributable: which landlord asked, which
provider/model answered, which tools ran with which arguments and results.

This table is simultaneously the safety trail and the future eval set
(prompt changes get replayed against real audited conversations). Rows are
append-only in spirit: nothing in the app ever updates or deletes them.
"""

import uuid

from django.db import models


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
    # Stamped on every row so answer quality is attributable to the exact
    # provider/model that produced it (they're runtime-configurable).
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
