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
    """

    class RuleType(models.TextChoices):
        MIN_BALANCE = "MIN_BALANCE", "Minimum balance"
        GRACE_PERIOD = "GRACE_PERIOD", "Late-payment grace period"
        LATE_FEE = "LATE_FEE", "Late fee"
        VENDOR_PREFERENCE = "VENDOR_PREFERENCE", "Preferred vendor"
        AUTO_RECORD_PAYMENT = "AUTO_RECORD_PAYMENT", "Auto-record payments"

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
