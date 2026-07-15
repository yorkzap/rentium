"""
The public, logged-out layer.

Two ideas, kept deliberately separate from the properties app:

  Showcase   A landlord's public page. Exists for every landlord (created
             lazily), but `is_public` DEFAULTS TO FALSE — a landlord who
             never opens Settings has no public presence whatsoever. Nothing
             here is a "profile"; it's a consent record with a URL attached.

  Inquiry    A prospective tenant's message about one showcased property.
             No account, no tenant profile, no lease. It reaches the landlord
             by email + in-app notification, and the landlord can convert it
             into a viewing Appointment in one click.

SlugHistory exists so a landlord can rename their URL without 404ing every
link they've ever shared — the old slug 301s to the new one, forever.

PRIVACY INVARIANT (enforced in serializers, not just convention): nothing in
this app may ever emit Property.address, Property.postal_code, exact
lat/lng, tenant names, lease data, or the landlord's personal email/phone.
The public sees a neighbourhood and a contact form. That's it.
"""

import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MinLengthValidator
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rentium.core.phone import PhoneField
from rentium.properties.models import Property
from rentium.users.models import LandlordProfile

# Slugs that would collide with real routes or be confusing/abusive.
RESERVED_SLUGS = {
    "admin",
    "api",
    "app",
    "about",
    "auth",
    "blog",
    "contact",
    "dashboard",
    "help",
    "invite",
    "l",
    "legal",
    "login",
    "logout",
    "media",
    "pricing",
    "privacy",
    "public",
    "rentium",
    "settings",
    "signup",
    "sitemap",
    "static",
    "support",
    "terms",
    "viewing",
    "www",
    # every province code, so /l/<slug> can never shadow /<province>/<city>
    "ab",
    "bc",
    "mb",
    "nb",
    "nl",
    "ns",
    "nt",
    "nu",
    "on",
    "pe",
    "qc",
    "sk",
    "yt",
}

slug_validator = RegexValidator(
    regex=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    message=_(
        "Use lowercase letters, numbers and hyphens only — e.g. raj-rentals. "
        "No spaces, no leading or trailing hyphen."
    ),
)


class Showcase(models.Model):
    """One per landlord. Their public page at /l/<slug>."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.OneToOneField(
        LandlordProfile, on_delete=models.CASCADE, related_name="showcase"
    )

    # --- The consent switch. This is the whole ballgame. ---
    is_public = models.BooleanField(
        _("Show my properties publicly"),
        default=False,
        help_text=_(
            "OFF by default and stays off until the landlord explicitly turns it "
            "on. While off, /l/<slug> 404s and none of their properties appear on "
            "any city page or in the sitemap."
        ),
    )

    slug = models.SlugField(
        _("Public URL"),
        max_length=60,
        unique=True,
        null=True,
        blank=True,
        validators=[slug_validator, MinLengthValidator(3)],
        help_text=_("Their page lives at /l/<slug>. Renameable; old slugs redirect."),
    )
    display_name = models.CharField(
        _("Display Name"),
        max_length=120,
        blank=True,
        help_text=_(
            "Shown publicly instead of their account name. Blank falls back to the "
            "account name — they may prefer 'McKenzie Rentals' to their legal name."
        ),
    )
    bio = models.TextField(
        _("About"),
        blank=True,
        max_length=1200,
        help_text=_("Optional. A few sentences for prospective tenants."),
    )
    photo = models.ImageField(
        _("Photo or Logo"),
        upload_to="showcase/%Y/%m/",
        null=True,
        blank=True,
    )

    # Where inquiries go. Falls back to the account email; kept separate so a
    # landlord can route tenant enquiries somewhere other than their login.
    contact_email = models.EmailField(_("Inquiry Email"), blank=True)

    first_published_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Showcase Page")
        verbose_name_plural = _("Showcase Pages")

    def __str__(self):
        state = "public" if self.is_public else "private"
        return f"/l/{self.slug or '(no slug)'} [{state}]"

    def clean(self):
        super().clean()
        if self.slug and self.slug.lower() in RESERVED_SLUGS:
            raise ValidationError({"slug": _("That URL is reserved. Pick another.")})
        if self.is_public and not self.slug:
            raise ValidationError(
                {"slug": _("Choose a public URL before making your page public.")}
            )

    def save(self, *args, **kwargs):
        if self.slug:
            self.slug = self.slug.strip().lower()
        if self.is_public and not self.first_published_at:
            self.first_published_at = timezone.now()
        self.full_clean(exclude=None if self.pk is None else [])
        super().save(*args, **kwargs)

    # -------------------------------------------------------------- helpers
    @property
    def public_name(self) -> str:
        return self.display_name or self.landlord.user.name or "Landlord"

    @property
    def inquiry_email(self) -> str:
        return self.contact_email or self.landlord.user.email

    def public_properties(self):
        """This landlord's showcased properties. Runs the ONE visibility rule."""
        return (
            Property.objects.public()
            .filter(landlord=self.landlord)
            .order_by("asking_rent", "name")
        )

    @classmethod
    def for_landlord(cls, landlord: LandlordProfile) -> "Showcase":
        """Lazily create. Created rows are PRIVATE — creation is not consent."""
        showcase, _created = cls.objects.get_or_create(landlord=landlord)
        return showcase


class ShowcaseSlugHistory(models.Model):
    """
    Every slug a showcase has ever had. Renaming is allowed precisely because
    the old URL keeps working (301 → the new one), so a landlord who put their
    link on a poster in March isn't punished for tidying it up in June.
    """

    slug = models.SlugField(max_length=60, unique=True, db_index=True)
    showcase = models.ForeignKey(
        Showcase, on_delete=models.CASCADE, related_name="slug_history"
    )
    retired_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Retired Showcase URL")
        verbose_name_plural = _("Retired Showcase URLs")
        ordering = ["-retired_at"]

    def __str__(self):
        return f"{self.slug} -> {self.showcase.slug}"


class Inquiry(models.Model):
    """
    "I'm interested in this place." No account required — that's the point.
    Carries only what the landlord needs to reply, and nothing that could
    identify a current tenant.
    """

    class Status(models.TextChoices):
        NEW = "NEW", _("New")
        REPLIED = "REPLIED", _("Replied")
        ARCHIVED = "ARCHIVED", _("Archived")
        SPAM = "SPAM", _("Spam")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(
        Property, on_delete=models.CASCADE, related_name="inquiries"
    )
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="inquiries"
    )

    name = models.CharField(_("Name"), max_length=150)
    email = models.EmailField(_("Email"))
    phone = PhoneField(_("Phone"))
    message = models.TextField(_("Message"), max_length=2000)
    move_in_target = models.DateField(_("Hoping to move in"), null=True, blank=True)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.NEW, db_index=True
    )
    landlord_notes = models.TextField(blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    # Abuse forensics only. Never exposed through any API.
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    # Once the landlord turns this into a showing, the two link up rather than
    # living as two disconnected records of the same person.
    appointment = models.ForeignKey(
        "appointments.Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inquiries",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Inquiry")
        verbose_name_plural = _("Inquiries")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["landlord", "status", "created_at"])]

    def __str__(self):
        return f"{self.name} re: {self.property.name} [{self.status}]"

    def mark_replied(self):
        self.status = self.Status.REPLIED
        self.responded_at = timezone.now()
        self.save(update_fields=["status", "responded_at"])

    def publish_event(self):
        from rentium.events.registry import publish

        publish(
            "inquiry.created",
            {
                "inquiry_id": str(self.pk),
                "property_name": self.property.name,
                "name": self.name,
                "email": self.email,
                "phone": self.phone,
                "message": self.message[:200],
            },
            property_id=self.property_id,
        )
