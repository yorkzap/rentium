"""
Areas: named spaces within a property or property group.

Private rooms already exist as Property rows inside a PropertyGroup — the
room IS the private area, so no Area object is needed for it. Areas model
everything else:

  - COMMON:    kitchen, shared bathroom, laundry, hallway, yard —
               accessible to every tenant in the group
  - EXCLUSIVE: an area reserved for one room (e.g. "the ensuite bathroom
               belongs to Room 1 only") via `exclusive_to`
  - SYSTEM:    furnace, roof, hot-water tank — landlord concern; tenants
               still see work orders on systems serving their unit

A tenant's covered territory = their room + the group's COMMON areas +
areas EXCLUSIVE to their room. Maintenance work orders reference an Area
so tenants see exactly the tickets that affect them.

IMPORTANT: add `from .areas import Area  # noqa` at the bottom of
rentium/properties/models.py so migrations pick this model up. If your
group model is not named `PropertyGroup`, adjust the import below.
"""

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from .models import Property, PropertyGroup


class Area(models.Model):
    class Kind(models.TextChoices):
        COMMON = "COMMON", _("Common / Shared")
        EXCLUSIVE = "EXCLUSIVE", _("Exclusive to a Room")
        SYSTEM = "SYSTEM", _("Building System")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.ForeignKey(
        PropertyGroup, on_delete=models.CASCADE, null=True, blank=True, related_name="areas"
    )
    property = models.ForeignKey(
        Property, on_delete=models.CASCADE, null=True, blank=True, related_name="areas",
        help_text=_("For standalone (non-group) units."),
    )
    name = models.CharField(_("Name"), max_length=100)
    kind = models.CharField(_("Kind"), max_length=10, choices=Kind.choices, default=Kind.COMMON)
    exclusive_to = models.ForeignKey(
        Property, on_delete=models.SET_NULL, null=True, blank=True, related_name="exclusive_areas",
        help_text=_("The room this area is reserved for (EXCLUSIVE kind only)."),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("Area")
        verbose_name_plural = _("Areas")
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(group__isnull=False, property__isnull=True)
                    | models.Q(group__isnull=True, property__isnull=False)
                ),
                name="area_belongs_to_group_xor_property",
            ),
        ]

    def __str__(self):
        parent = self.group or self.property
        return f"{self.name} ({parent})"

    def clean(self):
        super().clean()
        if self.kind == self.Kind.EXCLUSIVE and not self.exclusive_to_id:
            raise ValidationError({"exclusive_to": _("Exclusive areas must name their room.")})
        if self.kind != self.Kind.EXCLUSIVE and self.exclusive_to_id:
            raise ValidationError({"exclusive_to": _("Only EXCLUSIVE areas can be reserved for a room.")})
        if self.exclusive_to and self.group_id and self.exclusive_to.group_id != self.group_id:
            raise ValidationError({"exclusive_to": _("Room is not in this group.")})


DEFAULT_COMMON_AREAS = ["Kitchen", "Bathroom", "Living Room", "Laundry", "Hallway", "Exterior / Yard"]
DEFAULT_SYSTEM_AREAS = ["Heating / Furnace", "Hot Water", "Electrical Panel", "Roof / Structure"]


def seed_default_areas(*, group=None, property=None) -> list[Area]:
    """
    Call on property/group creation so landlords start with sensible areas
    they can rename or delete instead of a blank slate. Idempotent by name.
    """
    target = {"group": group} if group else {"property": property}
    created = []
    for name in DEFAULT_COMMON_AREAS:
        area, was_created = Area.objects.get_or_create(**target, name=name, defaults={"kind": Area.Kind.COMMON})
        if was_created:
            created.append(area)
    for name in DEFAULT_SYSTEM_AREAS:
        area, was_created = Area.objects.get_or_create(**target, name=name, defaults={"kind": Area.Kind.SYSTEM})
        if was_created:
            created.append(area)
    return created


def areas_for_tenant_room(room: Property):
    """The areas a tenant in `room` can see/report on."""
    q = models.Q(exclusive_to=room)
    if room.group_id:
        q |= models.Q(group_id=room.group_id, kind__in=[Area.Kind.COMMON, Area.Kind.SYSTEM])
    else:
        q |= models.Q(property=room, kind__in=[Area.Kind.COMMON, Area.Kind.SYSTEM])
    return Area.objects.filter(q)
