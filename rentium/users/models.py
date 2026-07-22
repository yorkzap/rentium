from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import CASCADE
from django.db.models import CharField
from django.db.models import EmailField
from django.db.models import ForeignKey
from django.db.models import OneToOneField
from django.db.models import TextChoices
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from rentium.core.phone import PhoneField

from .managers import UserManager


class User(AbstractUser):
    """
    Default custom user model for Rentium.
    If adding fields that need to be filled at user signup,
    check forms.SignupForm and forms.SocialSignupForms accordingly.
    """

    class UserType(TextChoices):
        LANDLORD = "LANDLORD", _("Landlord")
        TENANT = "TENANT", _("Tenant")

    # First and last name do not cover name patterns around the globe
    name = CharField(_("Name of User"), blank=True, max_length=255)
    first_name = None  # type: ignore[assignment]
    last_name = None  # type: ignore[assignment]
    email = EmailField(_("email address"), unique=True)
    username = None  # type: ignore[assignment]
    user_type = CharField(
        _("User Type"), max_length=8, choices=UserType.choices, null=True, blank=True
    )
    phone = PhoneField(_("Phone Number"))

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects: ClassVar[UserManager] = UserManager()

    def get_absolute_url(self) -> str:
        """Get URL for user's detail view.

        Returns:
            str: URL for user detail.

        """
        return reverse("users:detail", kwargs={"pk": self.id})


class LandlordProfile(models.Model):
    user = OneToOneField(User, on_delete=CASCADE, related_name="landlord_profile")
    province = CharField(_("Province"), max_length=100)
    country = CharField(_("Country"), max_length=100)
    # The landlord's local timezone. Every "5pm" they type — preferred viewing
    # hours, scheduled times — is interpreted here, and every time we show them
    # is rendered here. Default matches the launch market (BC). An IANA name.
    timezone = CharField(
        _("Timezone"),
        max_length=64,
        default="America/Vancouver",
        help_text=_("IANA timezone name, e.g. America/Vancouver."),
    )

    def __str__(self):
        return f"Landlord: {self.user.name}"


class TenantProfile(models.Model):
    user = OneToOneField(User, on_delete=CASCADE, related_name="tenant_profile")

    def __str__(self):
        return f"Tenant: {self.user.name}"


class LandlordTeamMember(models.Model):
    """A co-landlord / property manager granted access to an OWNER's portfolio.

    When `member` logs in, they can act on `owner`'s data. Access is resolved
    through one helper (users/access.py) so it can be applied surface-by-surface
    and always fails CLOSED — a surface that doesn't consult the helper simply
    scopes to the user's own profile and grants no extra access.

    Invited by email; `member` is linked once they have (or claim) an account.
    """

    import uuid as _uuid

    class Role(TextChoices):
        MANAGER = "MANAGER", _("Manager (full access)")

    id = models.UUIDField(primary_key=True, default=_uuid.uuid4, editable=False)
    owner = ForeignKey(
        LandlordProfile, on_delete=CASCADE, related_name="team_members"
    )
    member = ForeignKey(
        User,
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="co_landlord_memberships",
        help_text=_("Set once the invited person has an account."),
    )
    invited_email = EmailField(blank=True, default="")
    invited_name = CharField(max_length=150, blank=True, default="")
    role = CharField(max_length=20, choices=Role.choices, default=Role.MANAGER)
    # Scope of access. Both null = whole portfolio (legacy / office manager).
    # `property` set = that property + any siblings in its group. `group` set =
    # the whole group. A scoped co-landlord also becomes a co-signer on FUTURE
    # leases created on the scoped property/group (see leases.signals).
    scope_property = ForeignKey(
        "properties.Property",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="co_landlord_grants",
    )
    scope_group = ForeignKey(
        "properties.PropertyGroup",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="co_landlord_grants",
    )
    invite_token = models.UUIDField(default=_uuid.uuid4, editable=False)
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "member", "scope_property", "scope_group"],
                condition=models.Q(member__isnull=False),
                name="uniq_team_owner_member_scope",
            ),
        ]

    def __str__(self):
        who = self.member_id or self.invited_email or "?"
        return f"CoLandlord {who} → owner {self.owner_id}"
