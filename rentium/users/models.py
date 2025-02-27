from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import CharField, EmailField, ForeignKey, OneToOneField
from django.db.models import CASCADE, TextChoices
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .managers import UserManager


class User(AbstractUser):
    """
    Default custom user model for Rentium.
    If adding fields that need to be filled at user signup,
    check forms.SignupForm and forms.SocialSignupForms accordingly.
    """

    class UserType(TextChoices):
        LANDLORD = 'LANDLORD', _('Landlord')
        TENANT = 'TENANT', _('Tenant')
    
    # First and last name do not cover name patterns around the globe
    name = CharField(_("Name of User"), blank=True, max_length=255)
    first_name = None  # type: ignore[assignment]
    last_name = None  # type: ignore[assignment]
    email = EmailField(_("email address"), unique=True)
    username = None  # type: ignore[assignment]
    user_type = CharField(
        _("User Type"),
        max_length=8,
        choices=UserType.choices,
        null=True,
        blank=True
    )
    phone = CharField(_("Phone Number"), max_length=20, blank=True)

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
    
    def __str__(self):
        return f"Landlord: {self.user.name}"

class TenantProfile(models.Model):
    user = OneToOneField(User, on_delete=CASCADE, related_name="tenant_profile")
    
    def __str__(self):
        return f"Tenant: {self.user.name}"
