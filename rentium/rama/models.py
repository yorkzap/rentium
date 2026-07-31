"""
RAMA models.

- RamaPreferences: per-landlord opt-in + model choice (their RAMA, not a
  site-wide switch). Memory/audit is always scoped to this landlord.
- RamaAudit: append-only trail of messages, tool calls, and errors — also
  the per-conversation "memory" rebuilt into the next turn.
"""

import uuid

from django.db import models
from django.core.validators import MinValueValidator


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
    # Per-role model overrides (the CAF command structure). Empty → platform
    # RAMA_<ROLE>_* settings → the chat provider with the role's default tier
    # (see runtime.get_role_config). Corporals always use provider/model above.
    general_provider = models.CharField(
        max_length=40, blank=True, default="", choices=Provider.choices
    )
    general_model = models.CharField(max_length=100, blank=True, default="")
    # Per-role BYOK key — lets the General/FSA run a DIFFERENT provider than the
    # corporal (model-agnostic). Blank → fall back to the main key (if same
    # provider) or the platform key. Never returned in full by the API.
    general_api_key = models.CharField(max_length=512, blank=True, default="")
    fsa_provider = models.CharField(
        max_length=40, blank=True, default="", choices=Provider.choices
    )
    fsa_model = models.CharField(max_length=100, blank=True, default="")
    fsa_api_key = models.CharField(max_length=512, blank=True, default="")
    # The Treasurer is the one role with a provider opinion of its own
    # (runtime.ROLE_PREFERRED_PROVIDERS) — blank here means "use Gemini if the
    # platform can call it", not "use the corporal's model".
    treasurer_provider = models.CharField(
        max_length=40, blank=True, default="", choices=Provider.choices
    )
    treasurer_model = models.CharField(max_length=100, blank=True, default="")
    treasurer_api_key = models.CharField(max_length=512, blank=True, default="")
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


class RamaTask(models.Model):
    """Durable state for one user request.

    Chat prose is not workflow state.  A task records what RAMA is trying to
    do, the validated inputs it has collected, and the terminal outcome it
    reached.  The command engine is the only writer of task transitions.
    """

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        NEEDS_INPUT = "NEEDS_INPUT", "Needs input"
        READY = "READY", "Ready"
        AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION", "Awaiting confirmation"
        EXECUTING = "EXECUTING", "Executing"
        VERIFIED = "VERIFIED", "Verified"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    TERMINAL_STATUSES = frozenset({Status.VERIFIED, Status.FAILED, Status.CANCELLED})
    ALLOWED_TRANSITIONS = {
        Status.RECEIVED: {
            Status.NEEDS_INPUT,
            Status.READY,
            Status.AWAITING_CONFIRMATION,
            Status.EXECUTING,
            Status.FAILED,
            Status.CANCELLED,
        },
        Status.NEEDS_INPUT: {
            Status.READY,
            Status.AWAITING_CONFIRMATION,
            Status.CANCELLED,
            Status.FAILED,
        },
        Status.READY: {
            Status.AWAITING_CONFIRMATION,
            Status.EXECUTING,
            Status.CANCELLED,
            Status.FAILED,
        },
        Status.AWAITING_CONFIRMATION: {
            Status.READY,
            Status.EXECUTING,
            Status.CANCELLED,
            Status.FAILED,
        },
        Status.EXECUTING: {
            Status.AWAITING_CONFIRMATION,
            Status.VERIFIED,
            Status.FAILED,
        },
        Status.VERIFIED: set(),
        Status.FAILED: set(),
        Status.CANCELLED: set(),
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_tasks",
    )
    conversation_id = models.UUIDField(db_index=True)
    capability_key = models.CharField(max_length=120, db_index=True)
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.RECEIVED, db_index=True
    )
    input = models.JSONField(default=dict, blank=True)
    context = models.JSONField(default=dict, blank=True)
    outcome = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=160, blank=True, default="")
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["landlord", "conversation_id", "-created_at"],
                name="rama_task_conversation_idx",
            ),
            models.Index(
                fields=["landlord", "idempotency_key"],
                name="rama_task_idempotency_idx",
            ),
        ]

    def transition_to(self, status: str, *, outcome=None, error: str = "") -> None:
        if status == self.status:
            return
        if status not in self.ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(f"Invalid RAMA task transition: {self.status} -> {status}")
        self.status = status
        update_fields = ["status", "updated_at"]
        if outcome is not None:
            self.outcome = outcome
            update_fields.append("outcome")
        if error:
            self.error = error
            update_fields.append("error")
        self.save(update_fields=update_fields)

    def __str__(self):
        return f"{self.capability_key} ({self.status}) for {self.landlord_id}"


class RamaActionReceipt(models.Model):
    """Immutable evidence that a RAMA command completed and was verified."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_action_receipts",
    )
    task = models.ForeignKey(
        RamaTask,
        on_delete=models.PROTECT,
        related_name="receipts",
    )
    capability_key = models.CharField(max_length=120, db_index=True)
    idempotency_key = models.CharField(max_length=160)
    inputs = models.JSONField(default=dict)
    effects = models.JSONField(default=dict)
    entity_refs = models.JSONField(default=list, blank=True)
    verification = models.JSONField(default=dict)
    links = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["landlord", "idempotency_key"],
                name="rama_receipt_landlord_idempotency_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=["landlord", "capability_key", "-created_at"],
                name="rama_receipt_capability_idx",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("RAMA action receipts are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("RAMA action receipts are immutable.")

    def __str__(self):
        return f"{self.capability_key} receipt {self.pk}"


class RamaPendingPlan(models.Model):
    """The multi-step plan a turn previewed and is waiting to be confirmed.

    RAMA runs one independent agent turn per message and rebuilds "memory" from
    text only, so a weak model can't reliably re-issue the exact tool calls it
    previewed when the landlord says "yes" — it just re-previews (an infinite
    loop). The backend persists the whole plan here and executes it itself
    deterministically on affirmation, no model reconstruction required.

    Single-tool previews are one-step plans (operation="single") — one code
    path for every confirmation. One row per conversation (latest wins); the
    row is deleted on completion or cancellation, so a row exists IFF a plan
    is outstanding. Steps flagged requires_own_confirm (TOOL_META policy,
    e.g. terminate_lease) pause execution for their own explicit "yes" —
    status AWAITING_STEP_CONFIRM marks that pause.
    """

    class Status(models.TextChoices):
        PENDING_CONFIRM = "PENDING_CONFIRM", "Awaiting plan confirmation"
        AWAITING_STEP_CONFIRM = "AWAITING_STEP_CONFIRM", "Awaiting step confirmation"

    conversation_id = models.UUIDField(primary_key=True)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_pending_plans",
    )
    task = models.OneToOneField(
        RamaTask,
        on_delete=models.CASCADE,
        related_name="pending_plan",
        null=True,
        blank=True,
    )
    operation = models.CharField(max_length=60, default="single")
    summary = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.PENDING_CONFIRM
    )
    # Next step index to run (steps before it are settled).
    cursor = models.PositiveIntegerField(default=0)
    # Informational: targets that were excluded with reasons, for continuity.
    blocked = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "RAMA pending plan"
        verbose_name_plural = "RAMA pending plans"

    def __str__(self):
        return (
            f"pending plan {self.operation} ({self.status}) for "
            f"{self.landlord_id} ({self.conversation_id})"
        )


class RamaPlanStep(models.Model):
    """One guarded tool call inside a pending plan."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DONE = "DONE", "Done"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    plan = models.ForeignKey(
        RamaPendingPlan, on_delete=models.CASCADE, related_name="steps"
    )
    order = models.PositiveIntegerField()
    tool = models.CharField(max_length=100)
    capability_key = models.CharField(max_length=120, blank=True, default="")
    arguments = models.JSONField(default=dict)
    target_label = models.CharField(max_length=200, blank=True, default="")
    # Steps sharing an item_key belong to one target: if one fails the rest
    # are skipped (never delete a property whose lease termination failed).
    item_key = models.CharField(max_length=64, blank=True, default="")
    requires_own_confirm = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    result = models.JSONField(default=dict, blank=True)
    receipt = models.OneToOneField(
        RamaActionReceipt,
        on_delete=models.PROTECT,
        related_name="plan_step",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "order"], name="rama_plan_step_order_unique"
            )
        ]

    def __str__(self):
        return f"step {self.order}: {self.tool} ({self.status})"


class RamaConstitutionSection(models.Model):
    """One versioned section of a landlord's Constitution — the written policy
    the General reads verbatim and treats as authoritative.

    Append-only: an amendment creates version N+1 and deactivates version N
    (`supersedes` keeps the chain). Sections are prose (markdown) for nuance;
    the machine-enforceable subset lives in RamaConstitutionRule — sentinels
    never parse markdown.
    """

    class Origin(models.TextChoices):
        LANDLORD = "LANDLORD", "Landlord"
        GENERAL_PROPOSAL = "GENERAL_PROPOSAL", "General (approved proposal)"

    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_constitution_sections",
    )
    key = models.SlugField(max_length=60)  # balances / vendors / tenant-policies / workflows / …
    title = models.CharField(max_length=200)
    body_md = models.TextField(blank=True, default="")
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    origin = models.CharField(
        max_length=20, choices=Origin.choices, default=Origin.LANDLORD
    )
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["key", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["landlord", "key", "version"],
                name="rama_constitution_section_version_unique",
            )
        ]

    def __str__(self):
        state = "active" if self.is_active else "superseded"
        return f"{self.key} v{self.version} ({state}) for {self.landlord_id}"


class RamaConstitutionRule(models.Model):
    """The machine-readable subset of the Constitution that $0 sentinels
    enforce deterministically. Params shape per type:

    - MIN_BALANCE:        {"property_id": str|null, "amount": "5000.00"}
    - GRACE_PERIOD:       {"tenant_id": str|null, "days": 5}
    - LATE_FEE:           {"amount": "25.00"} or {"percent": 2, "after_days": 3}
    - VENDOR_PREFERENCE:  {"trade": "plumbing", "name": "...", "phone": "...",
                           "priority": 1}
    - AUTO_RECORD_PAYMENT:{"confidence": "propose"|"auto"}
    - AUTONOMY:           {"categories": ["inventory", "admin", "memory"],
                           "channels": ["web"], "max_per_turn": 3,
                           "max_per_day": 20}

    AUTONOMY is what lets RAMA act without asking. It lives here rather than in
    RamaPreferences on purpose: amending the Constitution is itself an
    own_confirm write, so the model cannot grant itself autonomy without the
    landlord confirming a policy amendment in its own dedicated step. Absent
    rule = nothing ever auto-runs.
    """

    class RuleType(models.TextChoices):
        MIN_BALANCE = "MIN_BALANCE", "Minimum balance"
        GRACE_PERIOD = "GRACE_PERIOD", "Late-payment grace period"
        LATE_FEE = "LATE_FEE", "Late fee"
        VENDOR_PREFERENCE = "VENDOR_PREFERENCE", "Preferred vendor"
        AUTO_RECORD_PAYMENT = "AUTO_RECORD_PAYMENT", "Auto-record payments"
        AUTONOMY = "AUTONOMY", "Act without asking (per category)"

    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_constitution_rules",
    )
    rule_type = models.CharField(max_length=30, choices=RuleType.choices)
    params = models.JSONField(default=dict)
    active = models.BooleanField(default=True)
    section = models.ForeignKey(
        RamaConstitutionSection,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rules",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rule_type", "created_at"]

    def __str__(self):
        return f"{self.rule_type} {self.params} ({'on' if self.active else 'off'})"


class RamaInsight(models.Model):
    """One FSA analysis of a Sergeant's finding — a fact-grounded problem
    paired with a recommendation, surfaced to the landlord (bell + linked
    channels) and tracked to resolution.

    `facts` is the deterministic pack the Sergeant computed (the audit
    trail — what actually triggered this); `analysis` is the FSA's phrased
    read of those facts. `proposal_conversation` is reserved for when an
    insight becomes an actionable plan (FSA currently reasons over
    read-only facts; wiring it to a plan happens once a role needs it).
    """

    class Severity(models.TextChoices):
        INFO = "INFO", "Info"
        WARN = "WARN", "Warning"
        URGENT = "URGENT", "Urgent"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ACKED = "ACKED", "Acknowledged"
        ACTIONED = "ACTIONED", "Actioned"
        DISMISSED = "DISMISSED", "Dismissed"

    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_insights",
    )
    kind = models.CharField(max_length=60)  # e.g. rama.sentinel.min_balance
    severity = models.CharField(
        max_length=10, choices=Severity.choices, default=Severity.INFO
    )
    facts = models.JSONField(default=dict)
    analysis = models.TextField(blank=True, default="")
    proposal_conversation = models.UUIDField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    source_event_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["landlord", "status", "-created_at"])]

    def __str__(self):
        return f"{self.kind} ({self.severity}/{self.status}) for {self.landlord_id}"


class RamaCapabilityGap(models.Model):
    """Something a landlord asked for that RAMA couldn't do — captured as a
    STRUCTURED gap (not code). This is the safe first half of "self-evolving":
    RAMA logs what was missing instead of failing silently, turning real usage
    into a reviewable backlog. A human (or, later, an LLM-drafted playbook under
    human review) builds the actual capability — nothing here ever runs code.
    """

    class Status(models.TextChoices):
        NEW = "NEW", "New"
        REVIEWED = "REVIEWED", "Reviewed"
        BUILT = "BUILT", "Built"
        DISMISSED = "DISMISSED", "Dismissed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_capability_gaps",
    )
    request = models.TextField(help_text="What the landlord wanted, in their words.")
    detail = models.TextField(
        blank=True, default="", help_text="Why it was blocked / what was missing."
    )
    # 'learn now' = the landlord explicitly asked us to build this → prioritise.
    prioritised = models.BooleanField(default=False)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.NEW, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-prioritised", "-created_at"]
        indexes = [models.Index(fields=["landlord", "status", "-created_at"])]

    def __str__(self):
        return f"Gap [{self.status}] {self.request[:60]}"


class RamaAttachmentBatch(models.Model):
    """The exact files attached to one chat composer send.

    A batch is conversation-owned and sealed when the message is sent.  Tools
    must receive its ID explicitly; there is intentionally no "all unused
    uploads for this landlord" fallback.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        SEALED = "SEALED", "Sealed"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_attachment_batches",
    )
    conversation_id = models.UUIDField(db_index=True)
    message_id = models.UUIDField(null=True, blank=True, db_index=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sealed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["landlord", "conversation_id", "-created_at"],
                name="rama_attachment_batch_idx",
            )
        ]

    def __str__(self):
        return f"RAMA attachment batch {self.pk} ({self.status})"


class RamaAttachment(models.Model):
    """A staged file with explicit classification and disposition."""

    class Classification(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Unknown"
        PROPERTY_PHOTO = "PROPERTY_PHOTO", "Property photo"
        DOCUMENT = "DOCUMENT", "Document"

    class Status(models.TextChoices):
        STAGED = "STAGED", "Staged"
        CLASSIFIED = "CLASSIFIED", "Classified"
        APPLIED = "APPLIED", "Applied"
        REJECTED = "REJECTED", "Rejected"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        RamaAttachmentBatch,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    original = models.FileField(upload_to="rama_attachments/%Y/%m/")
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=160, blank=True, default="")
    sha256 = models.CharField(max_length=64, db_index=True)
    size = models.PositiveBigIntegerField(default=0)
    sequence = models.PositiveIntegerField()
    classification = models.CharField(
        max_length=30,
        choices=Classification.choices,
        default=Classification.UNKNOWN,
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.STAGED, db_index=True
    )
    target_type = models.CharField(max_length=80, blank=True, default="")
    target_id = models.CharField(max_length=100, blank=True, default="")
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "sequence"],
                name="rama_attachment_batch_sequence_unique",
            )
        ]

    def __str__(self):
        return f"{self.original_filename} in {self.batch_id}"


class RamaUpload(models.Model):
    """A photo the landlord attached in a RAMA chat, STAGED until a tool (e.g.
    attach_photo_to_listing) consumes it. Landlord-scoped — a tool may only
    resolve an upload belonging to the same landlord, so an attached image can
    never cross accounts. `used_at` marks it consumed (single use)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_uploads",
    )
    image = models.ImageField(upload_to="rama_uploads/%Y/%m/")
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["landlord", "-created_at"])]

    def __str__(self):
        return f"RamaUpload {self.pk} for {self.landlord_id}"


class RamaDocument(models.Model):
    """A durable, searchable business record ingested by RAMA.

    The original is retained byte-for-byte for evidentiary purposes.  A PDF
    archival rendition is stored beside it for a uniform, human-browsable
    filing cabinet.  Extracted text and classification are metadata; neither
    mutates the source document.
    """

    class Kind(models.TextChoices):
        EXPENSE = "EXPENSE", "Expense / payable"
        NOTICE = "NOTICE", "Notice"
        MORTGAGE = "MORTGAGE", "Mortgage / financing"
        INSURANCE = "INSURANCE", "Insurance"
        LEASE = "LEASE", "Lease / tenancy"
        TAX = "TAX", "Tax record"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        # Deliberately NOT expense-like: a statement is a LIST of transactions,
        # so filing it as one expense posts a single invented charge for the
        # closing balance. It routes to the staging importer instead.
        BANK_STATEMENT = "BANK_STATEMENT", "Bank / card statement"
        OTHER = "OTHER", "Other document"

    class PaymentState(models.TextChoices):
        NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"
        PAID = "PAID", "Paid"
        UNPAID = "UNPAID", "Unpaid / not yet cleared"
        UNKNOWN = "UNKNOWN", "Needs confirmation"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"
        READY = "READY", "Ready to file"
        FILED = "FILED", "Filed"
        FAILED = "FAILED", "Processing failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.PROTECT,
        related_name="rama_documents",
    )
    holding = models.ForeignKey(
        "properties.PropertyHolding",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="documents",
        help_text="Physical/legal property this record concerns.",
    )
    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="documents",
        help_text="Optional rentable room/unit; holding is the normal filing scope.",
    )
    portfolio_wide = models.BooleanField(
        default=False,
        help_text="True only when the record genuinely concerns the whole portfolio.",
    )
    ledger_entry = models.OneToOneField(
        "ledger.LedgerEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="source_document",
    )
    original_file = models.FileField(
        upload_to="business_documents/inbox/%Y/%m/", max_length=500
    )
    archival_pdf = models.FileField(
        upload_to="", blank=True, default="", max_length=500
    )
    original_filename = models.CharField(max_length=255)
    canonical_filename = models.CharField(max_length=255, blank=True, default="")
    media_type = models.CharField(max_length=100, blank=True, default="")
    byte_size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
    expense_category = models.CharField(max_length=20, blank=True, default="")
    payment_state = models.CharField(
        max_length=20,
        choices=PaymentState.choices,
        default=PaymentState.NOT_APPLICABLE,
    )
    title = models.CharField(max_length=255, blank=True, default="")
    issuer = models.CharField(max_length=200, blank=True, default="")
    reference_number = models.CharField(max_length=120, blank=True, default="")
    document_date = models.DateField(null=True, blank=True, db_index=True)
    due_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    currency = models.CharField(max_length=3, default="CAD")
    ocr_text = models.TextField(blank=True, default="")
    extracted_data = models.JSONField(default=dict, blank=True)
    classification_confidence = models.DecimalField(
        max_digits=5, decimal_places=4, default=0
    )
    match_confidence = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    clarification_question = models.TextField(blank=True, default="")
    clarification_answer = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    failure_reason = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rama_documents_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    filed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["landlord", "sha256"],
                name="rama_document_landlord_sha256_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=["landlord", "status", "-created_at"],
                name="rama_doc_status_idx",
            ),
            models.Index(
                fields=["landlord", "holding", "document_date"],
                name="rama_doc_holding_idx",
            ),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.holding and self.holding.landlord_id != self.landlord_id:
            raise ValidationError({"holding": "Holding belongs to another landlord."})
        if self.property and self.property.landlord_id != self.landlord_id:
            raise ValidationError({"property": "Listing belongs to another landlord."})
        if (
            self.property
            and self.holding
            and self.property.holding_id != self.holding_id
        ):
            raise ValidationError(
                {"property": "Listing is not part of the selected holding."}
            )
        if self.portfolio_wide and (self.holding_id or self.property_id):
            raise ValidationError(
                {"portfolio_wide": "Portfolio-wide records cannot name a property."}
            )

    def __str__(self):
        return self.title or self.original_filename


class RamaDocumentEvent(models.Model):
    """Append-only chain of custody for an ingested business document."""

    class Kind(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        OCR_COMPLETED = "OCR_COMPLETED", "OCR completed"
        CLASSIFIED = "CLASSIFIED", "Classified"
        CLARIFIED = "CLARIFIED", "Clarified"
        FILED = "FILED", "Filed"
        EXPENSE_POSTED = "EXPENSE_POSTED", "Expense posted"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        RamaDocument, on_delete=models.PROTECT, related_name="events"
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    actor = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True
    )
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            from django.core.exceptions import ValidationError

            raise ValidationError("Document events are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError

        raise ValidationError("Document events are append-only.")


class RamaAutoAction(models.Model):
    """Receipt for something RAMA did without asking.

    Needed because neither existing store can answer "what did it just do, and
    can I take it back?": RamaPendingPlan is deleted the moment a plan finishes
    (plan_runner.run_plan), and RamaAudit is append-only JSON that cannot be
    marked as reversed.

    Every row carries its own inverse, captured at execution time from
    tool_meta's `undo` callable. Undo replays that inverse through the normal
    plan runner, so the invariant that only run_plan() may inject confirm=yes
    survives undo too.
    """

    class Status(models.TextChoices):
        DONE = "DONE", "Done"
        UNDONE = "UNDONE", "Undone"
        UNDO_FAILED = "UNDO_FAILED", "Undo failed"
        EXPIRED = "EXPIRED", "Too old to undo"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_auto_actions",
    )
    conversation_id = models.UUIDField(db_index=True)
    tool = models.CharField(max_length=100)
    arguments = models.JSONField(default=dict, blank=True)
    target_label = models.CharField(max_length=200, blank=True, default="")
    result = models.JSONField(default=dict, blank=True)
    # Which Constitution rule authorised this, so a landlord reviewing a
    # surprising action can see exactly what permission produced it.
    policy_rule_id = models.IntegerField(null=True, blank=True)
    undo_tool = models.CharField(max_length=100, blank=True, default="")
    undo_arguments = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DONE, db_index=True
    )
    undone_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["landlord", "status", "-created_at"])]

    def __str__(self):
        return f"{self.tool} {self.target_label} ({self.status})"

    @property
    def is_undoable(self) -> bool:
        from django.utils import timezone

        from .autonomy import AUTO_UNDO_TTL

        return (
            self.status == self.Status.DONE
            and bool(self.undo_tool)
            and (timezone.now() - self.created_at) <= AUTO_UNDO_TTL
        )


class RamaMemory(models.Model):
    """Durable per-landlord preferences that survive the conversation.

    Scope, deliberately narrow:

    - IN: preferences and standing directives the landlord stated. "Invoices go
      to my bookkeeper Dana." "Never viewings on Sundays." "Call the basement
      suite the Garden."
    - OUT, portfolio state. live_context() recomputes rents, counts, balances
      and statuses every single turn and is authoritative. A stored "Room C is
      $900" is a bug with a long fuse — it cannot be kept in sync, and it would
      compete with the truth.
    - OUT, conversation summaries. A summary of a turn where the model was
      wrong becomes *durable* wrongness. Within-conversation recall is already
      handled by service._tool_facts_note, and its dying at the end of the
      conversation is a feature.

    Append-only by supersession, like RamaConstitutionSection. The partial
    unique constraint on (landlord, key) WHERE status=ACTIVE is what makes that
    structural rather than conventional: two contradictory active memories
    cannot exist, and a write to an existing key MUST supersede.

    Precedence, stated in the prompt and tested:
        LIVE PORTFOLIO > THE CONSTITUTION > LANDLORD MEMORY > chat history
    """

    class Scope(models.TextChoices):
        PORTFOLIO = "PORTFOLIO", "Always relevant"
        ENTITY = "ENTITY", "Relevant to one property or lease"

    class Source(models.TextChoices):
        LANDLORD_EXPLICIT = "LANDLORD_EXPLICIT", "Landlord said to remember it"
        # Recorded but NEVER injected: nothing reaches the prompt that the
        # landlord did not explicitly say.
        LANDLORD_IMPLIED = "LANDLORD_IMPLIED", "Inferred (recorded, not used)"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUPERSEDED = "SUPERSEDED", "Superseded"
        FORGOTTEN = "FORGOTTEN", "Forgotten"
        EXPIRED = "EXPIRED", "Expired"

    MAX_BODY_CHARS = 400
    MAX_ACTIVE_PER_LANDLORD = 200

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_memories",
    )
    key = models.SlugField(max_length=80)
    body = models.TextField()
    scope = models.CharField(
        max_length=12, choices=Scope.choices, default=Scope.PORTFOLIO
    )
    entity_key = models.CharField(max_length=120, blank=True, default="")
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.LANDLORD_EXPLICIT
    )
    origin_conversation = models.UUIDField(null=True, blank=True)
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    # Set at write time by memory.personal_data_present(). Flags rows that a
    # tenant-erasure request would need to reach — see the rama_forget_subject
    # management command.
    contains_personal_data = models.BooleanField(default=False)
    pinned = models.BooleanField(default=False)
    use_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "RAMA memory"
        verbose_name_plural = "RAMA memories"
        ordering = ["-pinned", "-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["landlord", "key"],
                condition=models.Q(status="ACTIVE"),
                name="rama_memory_active_key_unique",
            )
        ]
        indexes = [models.Index(fields=["landlord", "status", "-updated_at"])]

    def __str__(self):
        return f"{self.key} ({self.status}) for {self.landlord_id}"

    def clean(self):
        from django.core.exceptions import ValidationError

        if len(self.body or "") > self.MAX_BODY_CHARS:
            raise ValidationError(
                f"A memory must be one short statement "
                f"(max {self.MAX_BODY_CHARS} characters)."
            )


class LandlordFinancialProfile(models.Model):
    """What the Treasurer may know about the landlord personally.

    Rental income is taxed on top of employment income, so a marginal rate is
    what turns "this deduction saves you money" into a number. But occupation
    and exact salary are among the most sensitive things this app could hold,
    and they are NOT what the calculation needs.

    So: bands rather than dollars, and `self_reported_marginal_rate` as the
    preferred input — it is both less revealing and more accurate than
    inferring a rate from a salary, and it is the number an accountant would
    hand over anyway.

    Nothing here is read unless `consented_at` is set. Absent consent the
    Treasurer omits every tax figure rather than guessing, and says why.
    """

    class IncomeBand(models.TextChoices):
        UNDER_50K = "UNDER_50K", "Under $50,000"
        B50_100K = "B50_100K", "$50,000 – $100,000"
        B100_150K = "B100_150K", "$100,000 – $150,000"
        B150_250K = "B150_250K", "$150,000 – $250,000"
        OVER_250K = "OVER_250K", "Over $250,000"
        PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY", "Prefer not to say"

    class Filing(models.TextChoices):
        SINGLE = "SINGLE", "Single"
        COUPLE_ONE_INCOME = "COUPLE_ONE_INCOME", "Couple, one income"
        COUPLE_TWO_INCOMES = "COUPLE_TWO_INCOMES", "Couple, two incomes"
        CORPORATION = "CORPORATION", "Held in a corporation"
        PARTNERSHIP = "PARTNERSHIP", "Partnership"

    landlord = models.OneToOneField(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="financial_profile",
        primary_key=True,
    )
    # The gate. Null means every field below is ignored, whatever it holds.
    consented_at = models.DateTimeField(null=True, blank=True)
    consent_scope = models.CharField(max_length=32, default="TAX_ESTIMATES_ONLY")
    occupation = models.CharField(max_length=120, blank=True, default="")
    employment_income_band = models.CharField(
        max_length=20, choices=IncomeBand.choices, blank=True, default=""
    )
    other_income_band = models.CharField(
        max_length=20, choices=IncomeBand.choices, blank=True, default=""
    )
    filing_situation = models.CharField(
        max_length=20, choices=Filing.choices, blank=True, default=""
    )
    # Blank falls back to LandlordProfile.province.
    tax_province = models.CharField(max_length=2, blank=True, default="")
    # The preferred input: one number, less sensitive than the three above.
    self_reported_marginal_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "landlord financial profile"
        verbose_name_plural = "landlord financial profiles"

    def __str__(self):
        state = "consented" if self.consented_at else "no consent"
        return f"Financial profile for {self.landlord_id} ({state})"

    @property
    def usable(self) -> bool:
        return self.consented_at is not None


class TaxRateTable(models.Model):
    """Dated tax rates, loaded by a human.

    Deliberately data rather than code: brackets change every year, and a rate
    hardcoded in a prompt or a constant goes stale silently — the worst
    failure mode, because the arithmetic still looks right.

    `tax.marginal_rate_estimate` refuses to fall back to a previous year. A
    missing table means the Treasurer says it cannot estimate tax for that
    year, which is recoverable; quoting last year's brackets as this year's is
    not.
    """

    class Kind(models.TextChoices):
        PERSONAL_INCOME_BRACKETS = "PERSONAL_INCOME_BRACKETS", "Personal income brackets"
        CCA_CLASSES = "CCA_CLASSES", "Capital cost allowance classes"
        GST_HST_RATE = "GST_HST_RATE", "GST/HST rate"
        CAPITAL_GAINS_INCLUSION = "CAPITAL_GAINS_INCLUSION", "Capital gains inclusion"

    jurisdiction = models.CharField(max_length=10)  # "CA-FED" | "CA-BC"
    tax_year = models.PositiveIntegerField()
    kind = models.CharField(max_length=32, choices=Kind.choices)
    payload = models.JSONField(default=dict)
    source_url = models.URLField(max_length=500, blank=True, default="")
    source_fetched_at = models.DateTimeField(null=True, blank=True)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    # Who vouched for these numbers. A rate table with no human behind it is
    # exactly what this model exists to prevent.
    loaded_by = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-tax_year", "jurisdiction"]
        constraints = [
            models.UniqueConstraint(
                fields=["jurisdiction", "tax_year", "kind"],
                name="tax_rate_table_unique_per_year",
            )
        ]

    def __str__(self):
        return f"{self.jurisdiction} {self.tax_year} {self.kind}"


class TreasurerFact(models.Model):
    """A financial fact the ledger does not have.

    "You're missing that we took $2,000 rent from another tenant for a year"
    is true, material, and unrecorded. It cannot go in RamaMemory — that store
    REFUSES money, dates and counts on purpose, because its rows are injected
    into every prompt for every role forever with no as-of date and no way to
    reconcile them. A number living there is a liability with a long fuse.

    A financial assertion is the opposite shape: scoped, dated, reconciled
    against the ledger at write time, and injected only into the analysis that
    needs it. Different lifecycle, different store.

    Corrections supersede rather than edit — the partial unique constraint on
    (landlord, key) WHERE status=ACTIVE makes two contradictory active facts
    structurally impossible, and `supersedes` keeps the chain so "what did it
    believe before, and why did it change?" stays answerable.
    """

    class Kind(models.TextChoices):
        LANDLORD_ASSERTED = "LANDLORD_ASSERTED", "The landlord told us"
        RESEARCHED = "RESEARCHED", "Researched, with a source"
        DERIVED = "DERIVED", "Computed from other facts"
        ESTIMATE = "ESTIMATE", "Estimated under stated assumptions"
        TAX_ASSUMPTION = "TAX_ASSUMPTION", "Tax assumption"

    class Confidence(models.TextChoices):
        STATED = "STATED", "Stated as fact"
        RESEARCHED = "RESEARCHED", "Verified against a source"
        ESTIMATED = "ESTIMATED", "Estimated"
        UNVERIFIED = "UNVERIFIED", "Unverified — excluded from totals"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUPERSEDED = "SUPERSEDED", "Superseded by a correction"
        RETRACTED = "RETRACTED", "Retracted"
        EXPIRED = "EXPIRED", "Expired"

    class Direction(models.TextChoices):
        """Which side of the books this belongs to.

        Reconciliation is undefined without it: $2,000/month means something
        different as income than as a cost.
        """

        INCOME = "INCOME", "Money in"
        EXPENSE = "EXPENSE", "Money out"
        NEUTRAL = "NEUTRAL", "Neither (a rate, a value, a count)"

    class Period(models.TextChoices):
        ONE_TIME = "ONE_TIME", "One time"
        MONTHLY = "MONTHLY", "Per month"
        ANNUAL = "ANNUAL", "Per year"

    MAX_STATEMENT_CHARS = 400

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="treasurer_facts",
    )
    key = models.SlugField(max_length=120)
    subject = models.CharField(max_length=200)
    statement = models.TextField()

    kind = models.CharField(max_length=20, choices=Kind.choices)
    confidence = models.CharField(max_length=12, choices=Confidence.choices)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    direction = models.CharField(
        max_length=8, choices=Direction.choices, default=Direction.NEUTRAL
    )

    value_numeric = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    value_unit = models.CharField(max_length=20, default="CAD")
    period = models.CharField(
        max_length=12, choices=Period.choices, default=Period.ONE_TIME
    )
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)

    # Scope mirrors LedgerEntry's shape so reconciliation is a direct join
    # rather than a translation layer that can drift.
    holding = models.ForeignKey(
        "properties.PropertyHolding",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    property = models.ForeignKey(
        "properties.Property",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    lease = models.ForeignKey(
        "leases.Lease",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    category = models.CharField(max_length=30, blank=True, default="")

    source_type = models.CharField(max_length=20, default="LANDLORD")
    source_conversation = models.UUIDField(null=True, blank=True)
    source_document = models.ForeignKey(
        "rama.RamaDocument",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    asserted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    # Computed by treasurer_facts.reconcile() at write time. This is what stops
    # a correction becoming a double-count: if the ledger already holds most of
    # what was asserted, the fact is SHOWN but excluded from totals.
    ledger_overlap_amount = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    double_count_risk = models.BooleanField(default=False)
    reconciled_at = models.DateTimeField(null=True, blank=True)

    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_by_role = models.CharField(max_length=20, default="landlord")
    contains_personal_data = models.BooleanField(default=False)

    class Meta:
        verbose_name = "treasurer fact"
        verbose_name_plural = "treasurer facts"
        ordering = ["-asserted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["landlord", "key"],
                condition=models.Q(status="ACTIVE"),
                name="treasurer_fact_active_key_unique",
            )
        ]
        indexes = [
            models.Index(fields=["landlord", "status", "-asserted_at"]),
            models.Index(fields=["landlord", "holding", "effective_from"]),
        ]

    def __str__(self):
        return f"{self.key} ({self.status}) for {self.landlord_id}"

    def clean(self):
        from django.core.exceptions import ValidationError

        if len(self.statement or "") > self.MAX_STATEMENT_CHARS:
            raise ValidationError(
                f"A fact must be one statement (max {self.MAX_STATEMENT_CHARS} chars)."
            )


class RamaDeliberation(models.Model):
    """One structured analysis, start to finish.

    Persisted rather than computed on the fly for three reasons: a landlord
    asking "why did it recommend that?" gets the whole chain, a run that
    pauses for information can resume, and a correction can re-run the parts
    that changed instead of everything.
    """

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        AWAITING_INFO = "AWAITING_INFO", "Waiting on the landlord"
        DONE = "DONE", "Done"
        FAILED = "FAILED", "Failed"
        SUPERSEDED = "SUPERSEDED", "Superseded by a re-run"
        ABORTED_BUDGET = "ABORTED_BUDGET", "Stopped at its budget"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="rama_deliberations",
    )
    topic = models.CharField(max_length=60)
    question = models.TextField()
    trigger = models.CharField(max_length=60, default="landlord_ask")
    holding = models.ForeignKey(
        "properties.PropertyHolding",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True
    )
    # Where to resume. Stages before it are settled.
    stage_cursor = models.PositiveIntegerField(default=0)
    calls_used = models.PositiveIntegerField(default=0)
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    insight = models.ForeignKey(
        "rama.RamaInsight",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    # Same trick the sergeants use: one analysis per topic per scope per week.
    dedupe_key = models.CharField(max_length=160, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["landlord", "status", "-created_at"])]

    def __str__(self):
        return f"{self.topic} ({self.status}) for {self.landlord_id}"


class RamaDeliberationStage(models.Model):
    """One step of the analysis, with what went in and what came out.

    `conversation_id` is the evidence that a stage really was its own bounded
    sub-turn rather than part of one long generation — which is what stops a
    weak model collapsing the sequence.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DONE = "DONE", "Done"
        RETRIED = "RETRIED", "Retried"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    deliberation = models.ForeignKey(
        RamaDeliberation, on_delete=models.CASCADE, related_name="stages"
    )
    order = models.PositiveIntegerField()
    stage = models.CharField(max_length=20)
    option_key = models.CharField(max_length=60, blank=True, default="")
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    conversation_id = models.UUIDField(null=True, blank=True)
    provider = models.CharField(max_length=40, blank=True, default="")
    model = models.CharField(max_length=100, blank=True, default="")
    input_artifact = models.JSONField(default=dict, blank=True)
    output_artifact = models.JSONField(default=dict, blank=True)
    raw_reply = models.TextField(blank=True, default="")
    # Contract failures: a missing required slot, an unverified web figure, a
    # figure token that resolves to nothing.
    violations = models.JSONField(default=list, blank=True)
    retries = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["deliberation", "order"], name="rama_stage_unique_order"
            )
        ]

    def __str__(self):
        return f"{self.stage}#{self.order} ({self.status})"


class RamaOption(models.Model):
    """One candidate course of action, and what we know about it."""

    class Status(models.TextChoices):
        CANDIDATE = "CANDIDATE", "Candidate"
        GATHERED = "GATHERED", "Facts gathered"
        SCORED = "SCORED", "Scored"
        BLOCKED = "BLOCKED", "Blocked"
        EXCLUDED = "EXCLUDED", "Excluded"

    deliberation = models.ForeignKey(
        RamaDeliberation, on_delete=models.CASCADE, related_name="options"
    )
    catalogue_key = models.CharField(max_length=60)
    label = models.CharField(max_length=200)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.CANDIDATE
    )
    excluded_reason = models.CharField(max_length=200, blank=True, default="")
    facts = models.JSONField(default=dict, blank=True)
    scores = models.JSONField(default=dict, blank=True)
    rank = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["rank", "catalogue_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["deliberation", "catalogue_key"],
                name="rama_option_unique_per_deliberation",
            )
        ]

    def __str__(self):
        return f"{self.catalogue_key} ({self.status})"


class TreasurerSource(models.Model):
    """A web page the Treasurer read, cached with its fetch date.

    Kept per landlord rather than globally so an erasure request can reach it,
    and because a cached page is evidence for a figure that landlord acted on.

    `excerpt` is what `research.verify_in_source` checks a number against. A
    figure that does not appear verbatim in the fetched text is downgraded and
    excluded from scoring — which is the single strongest guard against a
    confidently invented rebate amount.
    """

    class Status(models.TextChoices):
        FRESH = "FRESH", "Fresh"
        STALE = "STALE", "Past its expiry"
        DEAD = "DEAD", "Could not be fetched"
        BLOCKED = "BLOCKED", "Refused before fetching"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="treasurer_sources",
    )
    topic = models.CharField(max_length=60, db_index=True)
    query = models.CharField(max_length=300, blank=True, default="")
    url = models.URLField(max_length=500)
    title = models.CharField(max_length=300, blank=True, default="")
    domain = models.CharField(max_length=120, db_index=True)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    excerpt = models.TextField(blank=True, default="")
    fetched_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    http_status = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.FRESH, db_index=True
    )

    class Meta:
        ordering = ["-fetched_at"]
        indexes = [models.Index(fields=["landlord", "topic", "-fetched_at"])]

    def __str__(self):
        return f"{self.domain} ({self.topic})"

    @property
    def is_fresh(self) -> bool:
        from django.utils import timezone

        return self.status == self.Status.FRESH and self.expires_at > timezone.now()


class TreasurerRequest(models.Model):
    """Something the Treasurer needs from the landlord to finish an analysis.

    Created by PYTHON, never by the model — an unfilled required slot becomes
    a request. That matters: a model asked to "say what you need" will invent
    plausible-sounding needs, whereas a slot that a strict parser found empty
    is a fact.

    `why_it_matters` is generated from the sensitivity result, so it always
    states the real consequence ("this decides whether windows or the heat
    pump comes first") rather than a generic plea for more information.

    Relayed by the General verbatim as "Treasurer request: …". The Treasurer
    has no channel of its own; the chain of command is the point.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        RELAYED = "RELAYED", "Passed to the landlord"
        ANSWERED = "ANSWERED", "Answered"
        DECLINED = "DECLINED", "Declined"
        EXPIRED = "EXPIRED", "Expired"

    MAX_OPEN_PER_LANDLORD = 3
    TTL_DAYS = 14

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="treasurer_requests",
    )
    deliberation = models.ForeignKey(
        "rama.RamaDeliberation",
        null=True, blank=True, on_delete=models.CASCADE, related_name="requests",
    )
    # The TreasurerFact key this answer will fill, so an answer routes back
    # without the landlord having to say what it was for.
    fact_key = models.SlugField(max_length=120, blank=True, default="")
    question = models.TextField()
    why_it_matters = models.TextField(blank=True, default="")
    expected_unit = models.CharField(max_length=20, blank=True, default="")
    expected_period = models.CharField(max_length=12, blank=True, default="")
    # Does the analysis stop until this is answered, or continue provisionally?
    blocking = models.BooleanField(default=False)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    relayed_at = models.DateTimeField(null=True, blank=True)
    answer_text = models.TextField(blank=True, default="")
    answered_at = models.DateTimeField(null=True, blank=True)
    resulting_fact = models.ForeignKey(
        "rama.TreasurerFact",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    # Never ask the same thing twice.
    dedupe_key = models.CharField(max_length=180, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-blocking", "created_at"]
        indexes = [models.Index(fields=["landlord", "status", "created_at"])]

    def __str__(self):
        return f"{self.question[:60]} ({self.status})"

    @property
    def is_live(self) -> bool:
        from django.utils import timezone

        if self.status not in (self.Status.OPEN, self.Status.RELAYED):
            return False
        return self.expires_at is None or self.expires_at > timezone.now()
