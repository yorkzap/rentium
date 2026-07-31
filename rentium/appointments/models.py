"""
rentium/appointments/models.py

Adds the public-showing flow used by every serious rental platform:
prospective tenants (no account) submit a viewing REQUEST from a public
page; the landlord CONFIRMS it into a scheduled appointment or declines.

New in v2:
  - Status.REQUESTED — a lead awaiting the landlord's confirmation.
  - contact_email — how we reach an unregistered requester.

Setup (if not done in the previous round):
  rentium/appointments/{__init__.py, models.py, api.py, public_views.py, apps.py}
  INSTALLED_APPS += ["rentium.appointments"]
  python manage.py makemigrations appointments && migrate
"""

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _

from rentium.core.phone import PhoneField


class Appointment(models.Model):
    class Kind(models.TextChoices):
        VIEWING = "VIEWING", _("Viewing / Showing")
        INSPECTION = "INSPECTION", _("Move-in / Move-out Inspection")
        CONTRACTOR = "CONTRACTOR", _("Contractor / Maintenance Visit")
        OTHER = "OTHER", _("Other")

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", _("Requested (awaiting landlord)")
        AWAITING_REQUESTER = (
            "AWAITING_REQUESTER",
            _("Awaiting requester's reply (landlord proposed a time)"),
        )
        SCHEDULED = "SCHEDULED", _("Scheduled")
        COMPLETED = "COMPLETED", _("Completed")
        CANCELLED = "CANCELLED", _("Cancelled")

    # The negotiation state machine. REQUESTED = the ball is in the landlord's
    # court (a fresh request, or the requester countered back). AWAITING_REQUESTER
    # = the landlord proposed a different time and it's the requester's move.
    # A landlord may confirm or decline from EITHER pending state — they're
    # never forced to wait on a reply (they may have sorted it out by phone).
    TRANSITIONS = {
        Status.REQUESTED: {
            Status.SCHEDULED,
            Status.AWAITING_REQUESTER,
            Status.CANCELLED,
        },
        Status.AWAITING_REQUESTER: {
            Status.SCHEDULED,
            Status.REQUESTED,
            Status.CANCELLED,
        },
        Status.SCHEDULED: {Status.COMPLETED, Status.CANCELLED},
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
    }

    class TenantConsent(models.TextChoices):
        NOT_APPLICABLE = "NOT_APPLICABLE", _("No current tenant")
        PENDING = "PENDING", _("Asked — awaiting the tenant")
        OK = "OK", _("Tenant is fine with it")
        OBJECTED = "OBJECTED", _("Tenant raised a concern")

    class TimeClass(models.TextChoices):
        UNSET = "UNSET", _("Not classified")
        IN_HOURS = "IN_HOURS", _("Within preferred hours")
        OUT_OF_HOURS = "OUT_OF_HOURS", _("Outside preferred hours")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "users.LandlordProfile", on_delete=models.CASCADE, related_name="appointments"
    )
    property = models.ForeignKey(
        "properties.Property", on_delete=models.CASCADE, related_name="appointments"
    )
    lease = models.ForeignKey(
        "leases.Lease",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
        help_text=_(
            "Current tenancy affected (tenants on it see the appointment — their entry notice)."
        ),
    )
    work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
        help_text=_("For contractor visits: the work order being serviced."),
    )
    inspection = models.ForeignKey(
        "leases.ConditionInspection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
        help_text=_(
            "For move-in / move-out inspection walkthroughs: the condition "
            "report this visit is scheduling."
        ),
    )
    kind = models.CharField(max_length=15, choices=Kind.choices, default=Kind.VIEWING)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.SCHEDULED
    )
    starts_at = models.DateTimeField(_("Starts At"))
    ends_at = models.DateTimeField(_("Ends At"), null=True, blank=True)
    contact_name = models.CharField(
        _("Who's Coming"),
        max_length=200,
        blank=True,
        help_text=_("Prospective tenant, contractor/company, etc."),
    )
    contact_email = models.EmailField(
        _("Contact Email"),
        blank=True,
        help_text=_("For public viewing requests: how to reach the requester."),
    )
    contact_phone = PhoneField(_("Contact Phone"))
    notes = models.TextField(_("Notes"), blank=True)
    # Is the current agreed/proposed time inside the landlord's preferred hours?
    # Purely informational — it never blocks a booking, it just lets us tell the
    # landlord "heads up, this is outside your usual hours" when we ping them.
    time_class = models.CharField(
        max_length=15, choices=TimeClass.choices, default=TimeClass.UNSET
    )
    # For a showing at an OCCUPIED unit: what the current tenant said. Advisory —
    # a landlord can confirm over an OBJECTED tenant (they may have squared it
    # away directly), but we record it because entry over a stated objection can
    # carry legal weight.
    tenant_consent = models.CharField(
        max_length=15,
        choices=TenantConsent.choices,
        default=TenantConsent.NOT_APPLICABLE,
    )
    tenant_consent_notes = models.TextField(blank=True)
    # Capability token for the requester's status page (/viewing/status/<token>).
    # A prospective tenant has no account; whoever holds this token may read
    # this ONE appointment's status — nothing else. Never shown to tenants.
    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # Prospect open-tracking for /viewing/status/<token> (and public status API).
    # First/last open + count answer "have they seen the invite link?" without
    # email-pixel tracking (which is unreliable and privacy-hostile).
    prospect_link_first_opened_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("First time the prospect opened their status/invite link."),
    )
    prospect_link_last_opened_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Most recent time the prospect opened their status/invite link."),
    )
    prospect_link_open_count = models.PositiveIntegerField(
        default=0,
        help_text=_("How many times the status/invite page was loaded."),
    )
    # Invite email delivery (SendGrid/Anymail webhook + send path).
    class InviteEmailStatus(models.TextChoices):
        NONE = "NONE", _("No email")
        QUEUED = "QUEUED", _("Queued / sent to provider")
        DELIVERED = "DELIVERED", _("Delivered to inbox")
        OPENED = "OPENED", _("Email opened (pixel, optional)")
        BOUNCED = "BOUNCED", _("Bounced")
        DROPPED = "DROPPED", _("Dropped / blocked")
        DEFERRED = "DEFERRED", _("Deferred")
        FAILED = "FAILED", _("Send failed")

    invite_email_status = models.CharField(
        max_length=12,
        choices=InviteEmailStatus.choices,
        default=InviteEmailStatus.NONE,
        db_index=True,
    )
    invite_email_provider_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=_("Provider message id (e.g. SendGrid x-message-id)."),
    )
    invite_email_updated_at = models.DateTimeField(null=True, blank=True)
    invite_email_detail = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at"]
        verbose_name = _("Appointment")
        verbose_name_plural = _("Appointments")

    def __str__(self):
        return f"{self.get_kind_display()} at {self.property.name} — {self.starts_at:%Y-%m-%d %H:%M} [{self.status}]"

    # ------------------------------------------------------------------ fsm
    def transition_to(self, new_status: str, *, by=None):
        """Move to `new_status` if the FSM allows it, else raise
        IllegalTransition. Does not save related side effects — callers emit
        the matching event."""
        from rentium.core.fsm import transition

        return transition(self, "status", new_status, self.TRANSITIONS, by=by)

    def stamp_time_class(self):
        """Recompute time_class from the landlord's preferred hours for the
        current starts_at. Call after setting/changing starts_at."""
        from .services import classify_time

        self.time_class = classify_time(self.landlord, self.property, self.starts_at)

    def record_prospect_link_open(self):
        """Stamp that the prospect loaded their status page (invite link)."""
        from django.db.models import F
        from django.utils import timezone

        now = timezone.now()
        updates = {
            "prospect_link_last_opened_at": now,
            "prospect_link_open_count": F("prospect_link_open_count") + 1,
            "updated_at": now,
        }
        if self.prospect_link_first_opened_at is None:
            updates["prospect_link_first_opened_at"] = now
        type(self).objects.filter(pk=self.pk).update(**updates)
        self.refresh_from_db(
            fields=[
                "prospect_link_first_opened_at",
                "prospect_link_last_opened_at",
                "prospect_link_open_count",
                "updated_at",
            ]
        )

    def record_proposal(self, *, by: str, starts_at, message: str = ""):
        """Append one turn to the negotiation trail."""
        return self.proposals.create(
            proposed_by=by, starts_at=starts_at, message=(message or "")[:500]
        )

    def publish_event(self, event_type: str, **extra):
        from rentium.events.registry import publish

        payload = {
            "appointment_id": str(self.pk),
            "kind": self.kind,
            "status": self.status,
            "starts_at": self.starts_at.isoformat(),
            "time_class": self.time_class,
            "tenant_consent": self.tenant_consent,
            "contact_name": self.contact_name,
            "contact_email": self.contact_email,
            "public_token": str(self.public_token),
            "work_order_id": str(self.work_order_id) if self.work_order_id else None,
        }
        payload.update(extra)
        return publish(
            event_type,
            payload,
            property_id=self.property_id,
            lease_id=self.lease_id,
        )


class AppointmentProposal(models.Model):
    """
    One turn in the back-and-forth over WHEN a visit happens. Every proposed
    time — the original request, each landlord counter, each requester counter,
    a tenant's suggested alternate — is a row here, so the full negotiation is
    auditable and either side can see the history. The currently-agreed or
    currently-proposed time stays denormalised on Appointment.starts_at.
    """

    class By(models.TextChoices):
        LANDLORD = "LANDLORD", _("Landlord")
        REQUESTER = "REQUESTER", _("Prospective tenant")
        TENANT = "TENANT", _("Current tenant")

    appointment = models.ForeignKey(
        Appointment, on_delete=models.CASCADE, related_name="proposals"
    )
    proposed_by = models.CharField(max_length=10, choices=By.choices)
    starts_at = models.DateTimeField()
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_proposed_by_display()} → {self.starts_at:%Y-%m-%d %H:%M}"


class Weekday(models.IntegerChoices):
    """Matches Python's datetime.weekday(): Monday is 0, Sunday is 6."""

    MONDAY = 0, _("Monday")
    TUESDAY = 1, _("Tuesday")
    WEDNESDAY = 2, _("Wednesday")
    THURSDAY = 3, _("Thursday")
    FRIDAY = 4, _("Friday")
    SATURDAY = 5, _("Saturday")
    SUNDAY = 6, _("Sunday")


class AvailabilityWindow(models.Model):
    """
    A recurring weekly window when a landlord is open to viewings.

    Rows with ``property = NULL`` are the landlord's DEFAULT hours. Rows tied to
    a property are an OVERRIDE that *replaces* the default for that property —
    but only when at least one override row exists for it (see
    ``services.preferred_windows``). A property with no override rows inherits
    the default; a property with its own rows ignores the default entirely.

    These windows never *block* a booking — viewings always require an explicit
    landlord confirmation regardless. They only classify a requested time as
    in- or out-of-hours, to steer the requester's picker and to tell the
    landlord "this one's outside your usual hours" when we ping them.
    """

    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="availability_windows",
    )
    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="availability_windows",
        help_text=_("Set to override this landlord's default hours for one property."),
    )
    weekday = models.IntegerField(choices=Weekday.choices)
    # A one-off window for a SPECIFIC date (e.g. "only July 25, 2–4pm"). When
    # set, it overrides the recurring weekly hours for that single date only.
    # `weekday` is still stored (derived from the date) so indexes/constraints
    # hold, but the recurring matcher ignores rows where this is set.
    specific_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["weekday", "start_time"]
        indexes = [models.Index(fields=["landlord", "property", "weekday"])]
        constraints = [
            models.CheckConstraint(
                check=models.Q(end_time__gt=models.F("start_time")),
                name="availability_window_end_after_start",
            )
        ]

    def __str__(self):
        scope = self.property.name if self.property_id else "default"
        return (
            f"{self.get_weekday_display()} "
            f"{self.start_time:%H:%M}–{self.end_time:%H:%M} ({scope})"
        )
