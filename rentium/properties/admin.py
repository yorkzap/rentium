from django.contrib import admin

from .areas import Area
from .models import InventoryItem
from .models import Property
from .models import PropertyArea
from .models import PropertyGroup
from .models import PropertyImage
from .models import SharedInventoryItem


class PropertyImageInline(admin.TabularInline):
    model = PropertyImage
    extra = 1
    fields = ("image", "caption", "order")


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "address",
        "city",
        "property_category",
        "get_type_display",
        "status",
        "has_primary_image",
        "landlord",
    )
    list_filter = ("status", "property_category", "unit_type", "room_type", "city")
    search_fields = ("name", "address", "city", "landlord__user__name")
    readonly_fields = ("created_at", "updated_at")
    inlines = [PropertyImageInline]

    def has_primary_image(self, obj):
        return bool(obj.primary_image)

    has_primary_image.boolean = True
    has_primary_image.short_description = "Has Primary Image"

    def get_type_display(self, obj):
        if obj.property_category == Property.PropertyCategory.COMPLETE_UNIT:
            return obj.get_unit_type_display()
        elif obj.property_category == Property.PropertyCategory.ROOM:
            return obj.get_room_type_display()
        return ""

    get_type_display.short_description = "Type"

    def get_fieldsets(self, request, obj=None):
        common_fieldsets = [
            (
                None,
                {"fields": ("landlord", "name", "description", "property_category")},
            ),
            (
                "Location",
                {"fields": ("address", "city", "province", "postal_code", "country")},
            ),
            (
                "Images",
                {"fields": ("primary_image",)},
            ),
            ("Status", {"fields": ("status",)}),
            (
                "Timestamps",
                {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
            ),
        ]
        if (
            obj is None
            or obj.property_category == Property.PropertyCategory.COMPLETE_UNIT
        ):
            complete_unit_fieldset = (
                "Complete Unit Details",
                {
                    "fields": (
                        "unit_type",
                        "bedrooms",
                        "bathrooms",
                        "max_occupancy",
                        "square_footage",
                    ),
                    "classes": (
                        []
                        if obj
                        and obj.property_category
                        == Property.PropertyCategory.COMPLETE_UNIT
                        else ["collapse"],
                    ),
                },
            )
            common_fieldsets.insert(2, complete_unit_fieldset)
        if obj is None or obj.property_category == Property.PropertyCategory.ROOM:
            room_fieldset = (
                "Room Details",
                {
                    "fields": (
                        "room_type",
                        "total_washrooms",
                        "other_rooms",
                        "shared_with",
                    ),
                    "classes": (
                        []
                        if obj
                        and obj.property_category == Property.PropertyCategory.ROOM
                        else ["collapse"],
                    ),
                },
            )
            common_fieldsets.insert(2, room_fieldset)
        return common_fieldsets

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj:
            if obj.property_category == Property.PropertyCategory.COMPLETE_UNIT:
                for field in [
                    "room_type",
                    "total_washrooms",
                    "other_rooms",
                    "shared_with",
                ]:
                    if field in form.base_fields:
                        form.base_fields[field].widget.attrs["disabled"] = True
            elif obj.property_category == Property.PropertyCategory.ROOM:
                for field in [
                    "unit_type",
                    "bedrooms",
                    "bathrooms",
                    "max_occupancy",
                    "square_footage",
                ]:
                    if field in form.base_fields:
                        form.base_fields[field].widget.attrs["disabled"] = True
        return form


@admin.register(PropertyImage)
class PropertyImageAdmin(admin.ModelAdmin):
    list_display = ("id", "property", "caption", "order", "created_at")
    list_filter = ("property",)
    search_fields = ("property__name", "caption")
    ordering = ("property", "order", "created_at")


@admin.register(PropertyGroup)
class PropertyGroupAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "landlord",
        "total_occupancy",
        "current_occupancy",
        "occupancy_percentage",
    )
    search_fields = ("name", "landlord__user__name")
    autocomplete_fields = ["landlord"]


@admin.register(PropertyArea)
class PropertyAreaAdmin(admin.ModelAdmin):
    list_display = ("area_type", "property", "count", "shared_with_landlord")
    list_filter = ("area_type", "shared_with_landlord")
    search_fields = ("property__name",)
    autocomplete_fields = ["property", "shared_by"]


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ("name", "property", "quantity", "condition", "location_description")
    list_filter = ("condition",)
    search_fields = ("name", "property__name")
    autocomplete_fields = ["property"]


@admin.register(SharedInventoryItem)
class SharedInventoryItemAdmin(admin.ModelAdmin):
    list_display = ("name", "group", "quantity", "condition")
    list_filter = ("condition",)
    search_fields = ("name", "group__name")
    autocomplete_fields = ["group"]


@admin.register(Area)
class AreaAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "group", "property", "exclusive_to")
    list_filter = ("kind",)
    search_fields = ("name",)
    autocomplete_fields = ["group", "property", "exclusive_to"]
