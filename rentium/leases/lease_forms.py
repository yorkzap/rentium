"""
Form packs: extra PDFs that get signed alongside (or after) the lease itself.

A tenancy is executed against ONE document — the agreement rendered by
leases/documents.py. Real tenancies need more paper than that: a BC landlord
ending a tenancy by agreement needs RTB-8, a pet owner needs an addendum, a
strata needs its own form. `LeaseDocument` could already hold an uploaded file
but its `is_signed` is a bare boolean with no signer, no timestamp, no checksum
and no field coordinates — it records that someone *said* a document was signed,
which is not the same thing as evidence that they signed it.

The shape:

    LeaseFormTemplate         the catalogue entry (system RTB-8, or an upload)
      └── LeaseFormPlacement  reusable field boxes, in page fractions

    LeaseForm                 one template attached to one lease
      ├── placements_snapshot frozen copy of the placements at attach time
      ├── LeaseFormSigner     who must sign; carries the public sign token
      ├── LeaseFormSignature  immutable evidence, one per signature
      └── LeaseFormEvent      immutable lifecycle stream

Three rules are inherited from code that already exists here, not invented:

1. Freeze at execution. documents.capture_signed_document() snapshots the
   agreement and hashes it the moment a lease activates. A completed form gets
   the same treatment on its BYTES: stamp once, hash, never re-render. Editing
   the template afterwards cannot reach back into a signed document.
2. Append-only evidence. LeaseInviteEvent and RamaDocumentEvent both refuse to
   be updated or deleted. LeaseFormEvent and LeaseFormSignature do the same.
3. A form's STAGE is what makes it mean something. A pet addendum is signed
   with the lease and holds up activation; an RTB-8 is signed at the END of a
   tenancy and drives the move-out. Storing "which stage" as first-class data
   is what lets the lease FSM, the attention feed and RAMA all agree about what
   a given piece of paper is for.

Coordinates are stored as FRACTIONS of the page (0..1, origin top-left), never
points or pixels: the landlord places fields on a server-rendered page image at
whatever DPI and zoom their screen happened to use, and the stamp has to land in
the same place on the real PDF. Fractions are the only representation that
survives that trip unchanged.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class FormStage(models.TextChoices):
    """WHEN in a tenancy this form is signed — and therefore what it does."""

    # Signed as part of executing the lease. Holds up activation (see
    # form_services.blocking_forms and Lease.check_and_activate).
    WITH_LEASE = "WITH_LEASE", _("Signed with the lease")
    # Signed at any point during a live tenancy. Never blocks anything.
    ADDENDUM = "ADDENDUM", _("Addendum during the tenancy")
    # Signed to END a tenancy — RTB-8 and friends. Drives MoveOutRequest.
    MOVE_OUT = "MOVE_OUT", _("Signed to end the tenancy")
    # An upload we have not been told the purpose of. form_intel can SUGGEST a
    # stage from OCR, but only a human ever promotes a suggestion into `stage`.
    UNCLASSIFIED = "UNCLASSIFIED", _("Not classified yet")


class SignerRole(models.TextChoices):
    LANDLORD = "LANDLORD", _("Landlord")
    CO_LANDLORD = "CO_LANDLORD", _("Co-landlord")
    TENANT = "TENANT", _("Tenant")
    OTHER = "OTHER", _("Other party")


class LeaseFormTemplate(models.Model):
    """A blank form: either a system catalogue entry or a landlord's own PDF.

    `landlord` NULL means a system form every landlord can use (RTB-8). A set
    landlord means a private upload, visible only to them — the same split
    DocumentTag uses.
    """

    class Source(models.TextChoices):
        SYSTEM = "SYSTEM", _("System catalogue")
        CUSTOM = "CUSTOM", _("Landlord upload")

    class Availability(models.TextChoices):
        AVAILABLE = "AVAILABLE", _("Available")
        # Catalogued so the landlord can see it is coming, but not selectable:
        # we know the form exists and we have not shipped its file yet. Better
        # than an empty list, and much better than shipping a form we have not
        # verified against the issuing authority's current revision.
        COMING_SOON = "COMING_SOON", _("Coming soon")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="lease_form_templates",
        help_text=_("Null for a system form available to every landlord."),
    )
    code = models.CharField(
        _("Form Code"),
        max_length=40,
        blank=True,
        default="",
        db_index=True,
        help_text=_("Stable identifier for system forms, e.g. BC_RTB8."),
    )
    name = models.CharField(_("Form Name"), max_length=200)
    purpose = models.TextField(
        _("Purpose"),
        blank=True,
        default="",
        help_text=_(
            "One sentence saying what this form does. Shown in the picker and "
            "read by RAMA so it can tell a landlord what they are about to send."
        ),
    )
    jurisdiction = models.CharField(
        _("Jurisdiction"),
        max_length=10,
        blank=True,
        default="",
        help_text=_("Province code the form belongs to, e.g. BC. Blank = anywhere."),
    )
    source = models.CharField(
        max_length=10, choices=Source.choices, default=Source.CUSTOM
    )
    stage = models.CharField(
        max_length=20,
        choices=FormStage.choices,
        default=FormStage.UNCLASSIFIED,
        db_index=True,
    )
    availability = models.CharField(
        max_length=15,
        choices=Availability.choices,
        default=Availability.AVAILABLE,
        db_index=True,
    )
    binds_to = models.CharField(
        _("Drives Workflow"),
        max_length=20,
        blank=True,
        default="",
        help_text=_(
            "'moveout' means completing this form signs and accepts the linked "
            "MoveOutRequest. Blank means the form is evidence only."
        ),
    )

    # --- the file ---------------------------------------------------------
    # COMING_SOON rows deliberately have no file: they exist to say "this form
    # is known, it is not ready", not to serve bytes.
    file = models.FileField(
        _("Blank Form PDF"),
        upload_to="lease_form_templates/%Y/%m/",
        blank=True,
        null=True,
        max_length=500,
    )
    original_filename = models.CharField(max_length=255, blank=True, default="")
    sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)
    byte_size = models.PositiveBigIntegerField(default=0)
    page_count = models.PositiveSmallIntegerField(default=0)
    page_sizes = models.JSONField(
        default=list,
        blank=True,
        help_text=_("[{'width': 612, 'height': 792}, ...] in PDF points, per page."),
    )

    # --- what OCR thinks it is, which is never the same as what it IS ------
    ocr_text = models.TextField(blank=True, default="")
    suggested_stage = models.CharField(
        max_length=20, choices=FormStage.choices, blank=True, default=""
    )
    suggested_purpose = models.TextField(blank=True, default="")
    suggestion_signals = models.JSONField(default=dict, blank=True)

    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Lease Form Template")
        verbose_name_plural = _("Lease Form Templates")
        ordering = ["jurisdiction", "name"]
        constraints = [
            # Same content, same landlord, one row — the dedupe idiom
            # rama_document_landlord_sha256_unique already uses. System rows
            # (landlord NULL) are excluded because Postgres treats NULLs as
            # distinct anyway; `code` is their uniqueness key instead.
            models.UniqueConstraint(
                fields=["landlord", "sha256"],
                condition=models.Q(landlord__isnull=False) & ~models.Q(sha256=""),
                name="lease_form_template_landlord_sha256_unique",
            ),
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(landlord__isnull=True) & ~models.Q(code=""),
                name="lease_form_template_system_code_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["landlord", "-created_at"]),
        ]

    def __str__(self):
        return self.name

    @property
    def is_selectable(self) -> bool:
        """Whether a landlord can actually attach this form right now."""
        return (
            self.is_active
            and self.availability == self.Availability.AVAILABLE
            and bool(self.file)
        )

    def page_size(self, page: int) -> tuple[float, float]:
        """(width, height) in PDF points for one 0-based page index."""
        try:
            row = self.page_sizes[page]
        except (IndexError, TypeError, KeyError):
            raise ValidationError(
                _("This form has no page %(page)s.") % {"page": page + 1}
            )
        return float(row["width"]), float(row["height"])


class LeaseFormPlacement(models.Model):
    """One field box on a blank template, in page fractions.

    Placed once per template and inherited by every lease that attaches it —
    which is the whole point of a template. `signer_index` exists so a landlord
    can place "Tenant 2's signature" before Tenant 2 has been invited, let alone
    created an account; the box is bound to a real person at send time.
    """

    class Kind(models.TextChoices):
        SIGNATURE = "SIGNATURE", _("Signature")
        INITIALS = "INITIALS", _("Initials")
        DATE = "DATE", _("Date signed")
        NAME = "NAME", _("Name")
        TEXT = "TEXT", _("Text")
        CHECKBOX = "CHECKBOX", _("Checkbox")

    #: Fields the signer fills at signing time. Everything else is either
    #: prefilled from the lease or typed by the landlord before sending.
    SIGNER_SUPPLIED = {Kind.SIGNATURE, Kind.INITIALS, Kind.DATE}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        LeaseFormTemplate, on_delete=models.CASCADE, related_name="placements"
    )
    key = models.CharField(
        _("Field Key"),
        max_length=120,
        help_text=_(
            "Stable slug for this box. Taken from the PDF's own AcroForm field "
            "name when it has one, so a filled RTB-8 is traceable field by field."
        ),
    )
    label = models.CharField(max_length=200, blank=True, default="")
    page = models.PositiveSmallIntegerField(default=0)

    # Fractions of the page, origin TOP-LEFT (screen convention). The renderer
    # flips to PDF's bottom-left origin in exactly one place (form_render.stamp)
    # so the conversion cannot drift between callers.
    x = models.FloatField()
    y = models.FloatField()
    width = models.FloatField()
    height = models.FloatField()

    kind = models.CharField(max_length=15, choices=Kind.choices)
    signer_role = models.CharField(
        max_length=15, choices=SignerRole.choices, default=SignerRole.TENANT
    )
    signer_index = models.PositiveSmallIntegerField(
        default=0, help_text=_("0-based: which tenant/co-landlord this box belongs to.")
    )
    auto_source = models.CharField(
        _("Prefill From"),
        max_length=60,
        blank=True,
        default="",
        help_text=_(
            "Whitelisted lease field to prefill from, e.g. tenant.display_name. "
            "See form_services.AUTO_SOURCES — an unknown key is an error, never "
            "a silent blank."
        ),
    )
    required = models.BooleanField(default=True)
    font_size = models.FloatField(default=10.0)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = _("Lease Form Placement")
        verbose_name_plural = _("Lease Form Placements")
        ordering = ["page", "order", "y", "x"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "key"], name="lease_form_placement_key_unique"
            )
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.key} p{self.page + 1}"

    def clean(self):
        super().clean()
        for field in ("x", "y", "width", "height"):
            value = getattr(self, field)
            if value is None or not (0.0 <= float(value) <= 1.0):
                raise ValidationError(
                    {
                        field: _(
                            "Placements are stored as fractions of the page, so "
                            "%(field)s must be between 0 and 1."
                        )
                        % {"field": field}
                    }
                )
        if float(self.x) + float(self.width) > 1.0001:
            raise ValidationError({"width": _("This box runs off the right edge.")})
        if float(self.y) + float(self.height) > 1.0001:
            raise ValidationError({"height": _("This box runs off the bottom edge.")})

    def as_dict(self) -> dict:
        """The snapshot shape stored on LeaseForm.placements_snapshot."""
        return {
            "key": self.key,
            "label": self.label,
            "page": self.page,
            "x": float(self.x),
            "y": float(self.y),
            "width": float(self.width),
            "height": float(self.height),
            "kind": self.kind,
            "signer_role": self.signer_role,
            "signer_index": self.signer_index,
            "auto_source": self.auto_source,
            "required": self.required,
            "font_size": float(self.font_size),
        }


class LeaseForm(models.Model):
    """One template attached to one lease — the thing that actually gets signed."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        SENT = "SENT", _("Sent for signature")
        PARTIALLY_SIGNED = "PARTIAL", _("Partially signed")
        COMPLETED = "COMPLETED", _("Completed")
        VOID = "VOID", _("Void")

    class CreatedVia(models.TextChoices):
        WEB = "web", _("Dashboard")
        RAMA = "rama", _("RAMA chat")
        SYSTEM = "system", _("System workflow")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(
        "leases.Lease", on_delete=models.CASCADE, related_name="lease_forms"
    )
    template = models.ForeignKey(
        LeaseFormTemplate, on_delete=models.PROTECT, related_name="instances"
    )
    moveout_request = models.ForeignKey(
        "leases.MoveOutRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lease_forms",
        help_text=_("Set for MOVE_OUT forms — completing this signs that request."),
    )
    title = models.CharField(max_length=255)
    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    required = models.BooleanField(
        default=True,
        help_text=_("An optional form is tracked but never blocks or nags."),
    )
    # Set at attach time. A WITH_LEASE form attached to a lease that is ALREADY
    # active is recorded as an outstanding obligation but must never push that
    # lease backwards out of ACTIVE — so it is stored with blocks_activation
    # False. See form_services.attach_form.
    blocks_activation = models.BooleanField(default=False)

    placements_snapshot = models.JSONField(default=list, blank=True)
    values = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Non-signature field values, keyed by placement key."),
    )

    # --- the executed document -------------------------------------------
    executed_file = models.FileField(
        upload_to="lease_forms/executed/%Y/%m/",
        blank=True,
        null=True,
        max_length=500,
    )
    executed_sha256 = models.CharField(max_length=64, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)

    created_via = models.CharField(
        max_length=10, choices=CreatedVia.choices, default=CreatedVia.WEB
    )
    source_attachment_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_("RamaAttachment id when the PDF arrived through chat/Telegram."),
    )
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Lease Form")
        verbose_name_plural = _("Lease Forms")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["lease", "status"]),
            models.Index(fields=["status", "blocks_activation"]),
        ]

    def __str__(self):
        return f"{self.title} on lease {self.lease.lease_number or self.lease_id}"

    @property
    def stage(self) -> str:
        return self.template.stage

    @property
    def is_executed(self) -> bool:
        return self.status == self.Status.COMPLETED

    def placements_for(self, signer) -> list[dict]:
        """The snapshot rows this signer is responsible for."""
        return [
            row
            for row in self.placements_snapshot
            if row.get("signer_role") == signer.role
            and int(row.get("signer_index") or 0) == signer.order
        ]

    def save(self, *args, **kwargs):
        # The executed bytes are evidence. Once written they are the document,
        # and the same freeze-at-execution rule the lease snapshot follows
        # applies here: a completed form is not editable, only voidable.
        if self.pk and self.executed_sha256:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("executed_sha256", "status")
                .first()
            )
            if (
                previous
                and previous["executed_sha256"]
                and previous["executed_sha256"] != self.executed_sha256
            ):
                raise ValidationError(
                    _("A completed form's executed document cannot be replaced.")
                )
        return super().save(*args, **kwargs)


class LeaseFormSigner(models.Model):
    """One party who must sign one form.

    Materialised at SEND time, not attach time. That ordering is what answers
    "what if there is no invitee yet": the landlord places a box against
    TENANT/0 whenever they like, and the actual human — from the lease roster,
    or typed in by hand — is bound to it at the moment the link goes out.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    form = models.ForeignKey(LeaseForm, on_delete=models.CASCADE, related_name="signers")
    role = models.CharField(max_length=15, choices=SignerRole.choices)
    order = models.PositiveSmallIntegerField(
        default=0, help_text=_("Matches LeaseFormPlacement.signer_index.")
    )

    lease_tenant = models.ForeignKey(
        "leases.LeaseTenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="form_signers",
    )
    landlord_signatory = models.ForeignKey(
        "leases.LeaseLandlordSignatory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="form_signers",
    )
    user = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lease_form_signers",
    )
    name = models.CharField(max_length=200, blank=True, default="")
    email = models.EmailField(blank=True, default="")

    sign_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)

    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.TextField(blank=True, default="")
    required = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Lease Form Signer")
        verbose_name_plural = _("Lease Form Signers")
        ordering = ["form", "role", "order"]
        constraints = [
            models.UniqueConstraint(
                fields=["form", "role", "order"], name="lease_form_signer_slot_unique"
            )
        ]

    def __str__(self):
        return f"{self.display_name} ({self.get_role_display()}) on {self.form_id}"

    @property
    def display_name(self) -> str:
        """Resolution chain, same shape as LeaseTenant.display_name."""
        if self.lease_tenant_id:
            return self.lease_tenant.display_name
        if self.user_id and getattr(self.user, "name", ""):
            return self.user.name
        return self.name or self.email or _("Pending signer")

    @property
    def has_signed(self) -> bool:
        return self.signed_at is not None

    @property
    def token_is_live(self) -> bool:
        if self.declined_at or self.signed_at:
            return False
        if self.token_expires_at and self.token_expires_at < timezone.now():
            return False
        return True

    def sign_url(self, frontend_base_url: str) -> str:
        return f"{(frontend_base_url or '').rstrip('/')}/sign/{self.sign_token}"


class LeaseFormSignature(models.Model):
    """Immutable evidence that one party signed one form at one moment.

    Deliberately richer than LeaseTenant.has_signed, which is a boolean with a
    timestamp and nothing else. If a signature is ever disputed, the question is
    "who, when, from where, and against which bytes" — so all four are recorded
    here, and none of them can be edited afterwards.
    """

    class Method(models.TextChoices):
        TYPED = "TYPED", _("Typed name")
        DRAWN = "DRAWN", _("Drawn signature")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    form = models.ForeignKey(
        LeaseForm, on_delete=models.CASCADE, related_name="signatures"
    )
    signer = models.ForeignKey(
        LeaseFormSigner, on_delete=models.CASCADE, related_name="signatures"
    )
    typed_name = models.CharField(
        max_length=200,
        help_text=_("The legal name the signer typed. Required for both methods."),
    )
    method = models.CharField(max_length=10, choices=Method.choices)
    signature_png = models.ImageField(
        upload_to="lease_forms/signatures/%Y/%m/", blank=True, null=True
    )
    signed_at = models.DateTimeField(default=timezone.now)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    template_sha256 = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_("The blank form's checksum at the moment this was signed."),
    )
    values_sha256 = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_("Checksum of the filled values the signer was shown."),
    )

    class Meta:
        verbose_name = _("Lease Form Signature")
        verbose_name_plural = _("Lease Form Signatures")
        ordering = ["signed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["signer"], name="lease_form_signature_once_per_signer"
            )
        ]

    def __str__(self):
        return f"{self.typed_name} signed {self.form_id} at {self.signed_at}"

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(_("Signatures are immutable."))
        if not (self.typed_name or "").strip():
            raise ValidationError({"typed_name": _("A signer must state their name.")})
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Signatures are immutable."))


class LeaseFormEvent(models.Model):
    """Append-only lifecycle stream, mirroring LeaseInviteEvent."""

    class Kind(models.TextChoices):
        CREATED = "CREATED", _("Form attached")
        FIELDS_PLACED = "FIELDS_PLACED", _("Fields placed")
        SENT = "SENT", _("Sent for signature")
        REMINDED = "REMINDED", _("Reminder sent")
        LINK_OPENED = "LINK_OPENED", _("Signing link opened")
        SIGNED = "SIGNED", _("Signed")
        DECLINED = "DECLINED", _("Declined")
        COMPLETED = "COMPLETED", _("Fully executed")
        VOIDED = "VOIDED", _("Voided")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    form = models.ForeignKey(LeaseForm, on_delete=models.CASCADE, related_name="events")
    signer = models.ForeignKey(
        LeaseFormSigner,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    actor = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lease_form_events",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Lease Form Event")
        verbose_name_plural = _("Lease Form Events")
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["form", "kind", "-created_at"], name="lease_form_event_kind_idx"
            )
        ]

    def __str__(self):
        return f"{self.kind} on {self.form_id} at {self.created_at}"

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(_("Lease form events are immutable."))
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Lease form events are immutable."))
