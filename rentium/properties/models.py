# rentium/properties/models.py
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from rentium.users.models import LandlordProfile

# from rentium.users.models import User # Not directly used here


# --- PropertyGroup Model ---
class PropertyGroup(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="property_groups"
    )
    name = models.CharField(
        _("Group Name"),
        max_length=100,
        help_text=_("e.g., Unit 5 Shared Spaces, Basement Suite Rooms"),
    )
    description = models.TextField(_("Description"), blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Property Group")
        verbose_name_plural = _("Property Groups")
        ordering = ["landlord", "name"]
        unique_together = ("landlord", "name")

    def __str__(self):
        return f"{self.name} (Landlord: {self.landlord.user.name})"


# --- Property Model (MODIFIED - added related_name for shared_areas) ---
class Property(models.Model):
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

    # Common fields
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
    primary_image = models.ImageField(
        _("Primary Image"),
        upload_to="properties/primary/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Main property image shown in listings"),
    )
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

    # Relationship to Group (For Room Organization)
    group = models.ForeignKey(
        PropertyGroup,
        on_delete=models.SET_NULL,
        related_name="grouped_properties",
        null=True,
        blank=True,
        verbose_name=_("Property Group"),
        help_text=_("Group this room belongs to (if sharing common areas)"),
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        group_info = f" (Group: {self.group.name})" if self.group else ""
        category_display = self.get_property_category_display()
        type_display = ""
        if self.property_category == self.PropertyCategory.COMPLETE_UNIT:
            type_display = self.get_unit_type_display() or "Unit"
        elif self.property_category == self.PropertyCategory.ROOM:
            type_display = self.get_room_type_display() or "Room"

        return f"{self.name} - {type_display} at {self.address}{group_info}"

    class Meta:
        verbose_name = _("Property")
        verbose_name_plural = _("Properties")
        ordering = ["-created_at"]

    def clean(self):
        super().clean()
        is_room_property = self.property_category == self.PropertyCategory.ROOM

        if not is_room_property:  # Complete Unit Validations
            if not self.unit_type:
                raise ValidationError(
                    {"unit_type": _("Unit type required for Complete Units.")}
                )
            if self.group:
                raise ValidationError(
                    {"group": _("Complete units cannot belong to a property group.")}
                )
            # Check related areas - cannot have areas shared by others if it's a complete unit
            if self.pk:  # Only check for existing properties
                # Find areas primarily associated with this unit OR shared by this unit
                associated_areas = PropertyArea.objects.filter(
                    models.Q(property=self) | models.Q(shared_by=self)
                ).distinct()
                for area in associated_areas:
                    # An area linked to a Complete Unit cannot be shared by definition
                    # Check if its shared_by count > 1 OR if its primary property isn't this one
                    if area.shared_by.count() > 1 or (
                        area.shared_by.count() == 1 and area.shared_by.first() != self
                    ):
                        raise ValidationError(
                            _(
                                "Areas associated with a 'Complete Unit' cannot be shared by other properties. Check area: %(area_name)s"
                            )
                            % {"area_name": area.get_area_type_display()}
                        )
        else:  # Room Validations
            if not self.room_type:
                raise ValidationError({"room_type": _("Room type required for Rooms.")})

        # Group assignment consistency check
        if self.group and self.group.landlord != self.landlord:
            raise ValidationError(
                {
                    "group": _(
                        "Cannot assign property to a group owned by a different landlord."
                    )
                }
            )

    # --- @property methods remain useful for easy access ---
    @property
    def additional_images(self):
        # Note: related_name changed from propertyimage_set to property_images
        return self.property_images.all()

    @property
    def primary_areas(self):
        """Areas primarily associated (owned) by this property."""
        # Note: related_name changed from areas to primary_area_associations
        return self.primary_area_associations.all()

    @property
    def private_inventory_items(self):
        # Note: related_name changed from inventoryitem_set to inventory_items
        return self.inventory_items.all()

    @property
    def shared_inventory_items(self):
        """Inventory items shared via the group this property belongs to."""
        if self.group:
            # Note: related_name changed from shared_items to group_shared_inventory
            return self.group.group_shared_inventory.all()
        try:
            SharedInventoryItem  # Check if defined
        except NameError:
            return None
        return SharedInventoryItem.objects.none()

    # We also have access to `self.shared_areas` via the M2M related_name


# --- PropertyImage Model (MODIFIED related_name) ---
class PropertyImage(models.Model):
    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="property_images",  # Changed related_name
    )
    image = models.ImageField(_("Image"), upload_to="properties/additional/%Y/%m/")
    caption = models.CharField(_("Caption"), max_length=255, blank=True)
    order = models.PositiveIntegerField(_("Display Order"), default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Property Image")
        verbose_name_plural = _("Property Images")
        ordering = ["order", "created_at"]

    def __str__(self):
        return f"Image for {self.property.name} ({self.id})"


# --- PropertyArea Model (MAJOR CHANGES) ---
class PropertyArea(models.Model):
    class AreaType(models.TextChoices):
        KITCHEN = "KITCHEN", _("Kitchen")
        BATHROOM = "BATHROOM", _("Bathroom")
        LIVING_ROOM = "LIVING_ROOM", _("Living Room")
        DINING_ROOM = "DINING_ROOM", _("Dining Room")
        BEDROOM = "BEDROOM", _("Bedroom")  # Could be private or shared common bedroom?
        LAUNDRY = "LAUNDRY", _("Laundry Area")
        OFFICE = "OFFICE", _("Office/Den")
        BALCONY = "BALCONY", _("Balcony/Patio")
        HALLWAY = "HALLWAY", _("Hallway/Entryway")
        STORAGE = "STORAGE", _("Storage Area")
        GARAGE = "GARAGE", _("Garage")
        GARDEN = "GARDEN", _("Garden/Yard")
        OTHER = "OTHER", _("Other")

    # Primary association - which property "owns" this area record?
    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="primary_area_associations",  # Changed related_name
        help_text=_("The primary property this area belongs to."),
    )
    area_type = models.CharField(
        _("Area Type"), max_length=20, choices=AreaType.choices
    )
    count = models.PositiveIntegerField(
        _("Count"), default=1, help_text=_("e.g., Number of identical areas")
    )
    description = models.TextField(
        _("Area Description"), blank=True, help_text=_("Optional details")
    )

    # NEW: ManyToMany field to define which properties share this specific area
    shared_by = models.ManyToManyField(
        Property,
        related_name="shared_areas",  # Properties can access areas they share
        blank=True,  # Can be empty if not shared (private to 'property')
        verbose_name=_("Shared By Properties"),
        help_text=_("Select ROOM properties that share access to this area."),
    )
    # REMOVED: is_shared (boolean)
    # REMOVED: is_private_to_room (boolean)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Property Area")
        verbose_name_plural = _("Property Areas")
        ordering = ["property", "area_type"]
        # unique_together removed as an area type might be shared differently now
        # unique_together = ("property", "area_type") # Reconsider if needed

    def __str__(self):
        # The M2M field isn't available until saved, so str might not reflect sharing status accurately immediately
        share_count = self.shared_by.count() if self.pk else 0
        status = ""
        if share_count == 1 and self.shared_by.first() == self.property:
            status = " (Private)"  # Only shared by its primary property
        elif share_count > 0:
            status = f" (Shared by {share_count} properties)"
        else:
            status = (
                " (Private - not explicitly shared)"  # Default if shared_by is empty
            )

        return f"{self.get_area_type_display()} ({self.count}) in {self.property.name}{status}"

    def clean(self):
        """
        Basic clean. Complex M2M validation (group consistency) is better
        handled in the serializer or view after M2M save.
        """
        super().clean()
        # Ensure the primary property isn't a COMPLETE_UNIT if trying to share
        # (More robust checks needed post-M2M save)
        if hasattr(self, "property") and self.property:
            if (
                self.property.property_category
                == Property.PropertyCategory.COMPLETE_UNIT
            ):
                # Check if shared_by has entries during update (can't check on create easily here)
                if (
                    self.pk
                    and self.shared_by.exists()
                    and (
                        self.shared_by.count() > 1
                        or self.shared_by.first() != self.property
                    )
                ):
                    raise ValidationError(
                        _(
                            "Areas primarily associated with a 'Complete Unit' cannot be shared by other properties."
                        )
                    )


# --- InventoryItem Model (Private - MODIFIED related_name) ---
class InventoryItem(models.Model):
    class ItemCondition(models.TextChoices):
        NEW = "NEW", _("New")
        GOOD = "GOOD", _("Good")
        FAIR = "FAIR", _("Fair")
        POOR = "POOR", _("Poor")
        DAMAGED = "DAMAGED", _("Damaged")
        MISSING = "MISSING", _("Missing")

    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="inventory_items",  # Changed related_name
    )
    name = models.CharField(
        _("Item Name"), max_length=200, help_text=_("e.g., Bedside Lamp")
    )
    description = models.TextField(_("Description/Notes"), blank=True)
    quantity = models.PositiveIntegerField(_("Quantity"), default=1)
    condition = models.CharField(
        _("Condition"),
        max_length=10,
        choices=ItemCondition.choices,
        blank=True,
        null=True,
    )
    location_description = models.CharField(
        _("Location Description"),
        max_length=255,
        blank=True,
        help_text=_("e.g., Bedroom Closet"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Private Inventory Item")
        verbose_name_plural = _("Private Inventory Items")
        ordering = ["property", "location_description", "name"]

    def __str__(self):
        return f"{self.name} (Qty: {self.quantity}) in {self.property.name}"


# --- SharedInventoryItem Model (New - MODIFIED related_name) ---
class SharedInventoryItem(models.Model):
    class ItemCondition(models.TextChoices):  # Keep consistent choices
        NEW = "NEW", _("New")
        GOOD = "GOOD", _("Good")
        FAIR = "FAIR", _("Fair")
        POOR = "POOR", _("Poor")
        DAMAGED = "DAMAGED", _("Damaged")
        MISSING = "MISSING", _("Missing")

    group = models.ForeignKey(
        PropertyGroup,
        on_delete=models.CASCADE,
        related_name="group_shared_inventory",  # Changed related_name
    )
    name = models.CharField(
        _("Item Name"), max_length=200, help_text=_("e.g., Microwave Oven")
    )
    description = models.TextField(_("Description/Notes"), blank=True)
    quantity = models.PositiveIntegerField(_("Quantity"), default=1)
    condition = models.CharField(
        _("Condition"),
        max_length=10,
        choices=ItemCondition.choices,
        blank=True,
        null=True,
    )
    location_description = models.CharField(
        _("Location Description"),
        max_length=255,
        blank=True,
        help_text=_("e.g., Kitchen Counter"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Shared Inventory Item")
        verbose_name_plural = _("Shared Inventory Items")
        ordering = ["group", "location_description", "name"]

    def __str__(self):
        return f"{self.name} (Qty: {self.quantity}) (Shared in {self.group.name})"
