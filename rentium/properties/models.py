from django.db import models
from django.utils.translation import gettext_lazy as _

from rentium.users.models import LandlordProfile


class Property(models.Model):
    """
    Represents a rental property owned by a landlord.
    Properties can be either complete units or individual rooms.
    """

    class PropertyCategory(models.TextChoices):
        COMPLETE_UNIT = "COMPLETE_UNIT", _("Complete Unit")
        ROOM = "ROOM", _("Room")

    class UnitType(models.TextChoices):
        BASEMENT = "BASEMENT", _("Basement")
        GARDEN_SUITE = "GARDEN_SUITE", _("Garden Suite")
        MAIN_FLOOR = "MAIN_FLOOR", _("Main Floor")
        APARTMENT = "APARTMENT", _("Apartment")
        OTHER = "OTHER", _("Other")

    class RoomType(models.TextChoices):
        PRIVATE = "PRIVATE", _("Private Room")
        SHARED = "SHARED", _("Shared Room")
        OTHER = "OTHER", _("Other")

    class PropertyStatus(models.TextChoices):
        AVAILABLE = "AVAILABLE", _("Available")
        OCCUPIED = "OCCUPIED", _("Occupied")
        MAINTENANCE = "MAINTENANCE", _("Under Maintenance")
        NOT_AVAILABLE = "NOT_AVAILABLE", _("Not Available")

    # Common fields for all property types
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="properties"
    )
    name = models.CharField(_("Property Name"), max_length=255)
    description = models.TextField(_("Description"), blank=True)
    address = models.CharField(_("Address"), max_length=255)
    city = models.CharField(_("City"), max_length=100)
    province = models.CharField(_("Province/State"), max_length=100)
    postal_code = models.CharField(_("Postal/Zip Code"), max_length=20)
    country = models.CharField(_("Country"), max_length=100)
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=PropertyStatus.choices,
        default=PropertyStatus.AVAILABLE,
    )

    # Property categorization
    property_category = models.CharField(
        _("Property Category"), max_length=20, choices=PropertyCategory.choices
    )

    # Fields for Complete Units
    unit_type = models.CharField(
        _("Unit Type"), max_length=20, choices=UnitType.choices, null=True, blank=True
    )
    bedrooms = models.IntegerField(_("Number of Bedrooms"), null=True, blank=True)
    bathrooms = models.DecimalField(
        _("Number of Bathrooms"), max_digits=3, decimal_places=1, null=True, blank=True
    )
    max_occupancy = models.IntegerField(_("Maximum Occupancy"), null=True, blank=True)
    square_footage = models.IntegerField(_("Square Footage"), null=True, blank=True)

    # Fields for Rooms
    room_type = models.CharField(
        _("Room Type"), max_length=20, choices=RoomType.choices, null=True, blank=True
    )
    total_washrooms = models.IntegerField(
        _("Total Washrooms in Property"), null=True, blank=True
    )
    other_rooms = models.TextField(
        _("Other Rooms Available"),
        blank=True,
        help_text=_("Describe other rooms that tenants have access to"),
    )
    shared_with = models.TextField(
        _("Shared With"),
        blank=True,
        help_text=_("Describe who else lives in the property"),
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        if self.property_category == self.PropertyCategory.COMPLETE_UNIT:
            return f"{self.name} - {self.get_unit_type_display()} at {self.address}"
        else:
            return f"{self.name} - {self.get_room_type_display()} at {self.address}"

    class Meta:
        verbose_name = _("Property")
        verbose_name_plural = _("Properties")
        ordering = ["-created_at"]

    def clean(self):
        """
        Ensure that fields relevant to the property category are properly filled out.
        """
        from django.core.exceptions import ValidationError

        if self.property_category == self.PropertyCategory.COMPLETE_UNIT:
            if not self.unit_type:
                raise ValidationError(
                    {"unit_type": _("Unit type is required for complete units")}
                )

        elif self.property_category == self.PropertyCategory.ROOM:
            if not self.room_type:
                raise ValidationError(
                    {"room_type": _("Room type is required for room rentals")}
                )
