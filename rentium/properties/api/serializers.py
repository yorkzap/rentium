from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from rentium.users.models import LandlordProfile

from ..models import InventoryItem
from ..models import Property
from ..models import PropertyArea
from ..models import PropertyGroup
from ..models import PropertyImage
from ..models import SharedInventoryItem


# --- PropertyImageSerializer ---
class PropertyImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = PropertyImage
        fields = ["id", "image", "caption", "order", "created_at"]
        read_only_fields = ["id", "created_at"]


# --- InventoryItemSerializer (Private) ---
class InventoryItemSerializer(serializers.ModelSerializer):
    condition_display = serializers.CharField(
        source="get_condition_display", read_only=True, allow_null=True
    )

    class Meta:
        model = InventoryItem
        fields = [
            "id",
            "property",
            "name",
            "description",
            "quantity",
            "condition",
            "condition_display",
            "location_description",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "condition_display"]
        extra_kwargs = {"property": {"write_only": True, "required": False}}


# --- SharedInventoryItemSerializer ---
class SharedInventoryItemSerializer(serializers.ModelSerializer):
    condition_display = serializers.CharField(
        source="get_condition_display", read_only=True, allow_null=True
    )
    group_name = serializers.CharField(source="group.name", read_only=True)

    class Meta:
        model = SharedInventoryItem
        fields = [
            "id",
            "group",
            "group_name",
            "name",
            "description",
            "quantity",
            "condition",
            "condition_display",
            "location_description",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "group_name",
            "created_at",
            "updated_at",
            "condition_display",
        ]
        extra_kwargs = {"group": {"write_only": True, "required": False}}


# --- BasicPropertySerializer (Minimal for Area relation) ---
class BasicPropertySerializer(serializers.ModelSerializer):
    class Meta:
        model = Property
        fields = ["id", "name", "property_category"]  # Include category for validation


# --- PropertyAreaSerializer (Validation Shifted to `validate`) ---
class PropertyAreaSerializer(serializers.ModelSerializer):
    area_type_display = serializers.CharField(
        source="get_area_type_display", read_only=True
    )
    # Writable field for M2M using Property IDs
    shared_by = serializers.PrimaryKeyRelatedField(
        # CHANGE: Use a broader queryset here, validation moved to `validate`
        queryset=Property.objects.all(),  # Allow any valid Property PK initially
        many=True,
        required=False,
        help_text=_("List of Property IDs sharing this area."),
    )
    # Read-only field for displaying details of shared properties (optional)
    shared_by_details = BasicPropertySerializer(
        source="shared_by", many=True, read_only=True
    )

    class Meta:
        model = PropertyArea
        fields = [
            "id",
            "property",  # Primary associated property (FK)
            "area_type",
            "area_type_display",
            "count",
            "description",
            "shared_by",  # Writable list of IDs
            "shared_by_details",  # Read-only nested details
            "shared_with_landlord",
            "is_group_common",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "area_type_display",
            "shared_by_details",
            "is_group_common",
        ]
        extra_kwargs = {
            "property": {
                "write_only": True,
                "required": False,
            },
        }

    # No __init__ needed for dynamic queryset anymore

    def validate(self, data):
        """
        Validate shared_by list, group consistency, category, and ownership.
        """
        instance = getattr(self, "instance", None)
        request = self.context.get("request")
        primary_property = self.context.get(
            "property"
        )  # Property from URL context (e.g., Prop 2)

        if not primary_property:
            raise serializers.ValidationError(
                _("API Error: Primary property context is missing.")
            )
        if not request or not hasattr(request.user, "landlord_profile"):
            raise serializers.ValidationError(
                _("API Error: User or landlord profile context is missing.")
            )

        current_landlord = request.user.landlord_profile

        # Get the list of Property *instances* passed for 'shared_by'
        # DRF's PrimaryKeyRelatedField already converted IDs [9, 2] to objects here,
        # using the broad Property.objects.all() queryset.
        shared_by_properties = data.get("shared_by", [])
        if instance and "shared_by" not in data:  # Handle PATCH case
            shared_by_properties = list(instance.shared_by.all())

        print(f"\n[DEBUG] PropertyAreaSerializer Validate:")
        print(
            f"  Context (Primary) Property: {primary_property.id} ({primary_property.name})"
        )
        print(
            f"  Incoming 'shared_by' properties (resolved): {[p.id for p in shared_by_properties]}"
        )

        if not shared_by_properties:
            print("  Validation: shared_by is empty (Private Area).")
            # If primary is COMPLETE_UNIT, this is fine.
            # If primary is ROOM, this is fine (private to room).
            return data  # Valid private state

        # --- Start M2M Validation ---
        print("  Validation: Running M2M checks for sharing...")
        target_group = None
        first_prop_checked = False

        # Get the expected group from the primary property (if it has one)
        expected_group = primary_property.group
        if (
            not expected_group
            and primary_property.property_category == Property.PropertyCategory.ROOM
        ):
            # If the primary property is a ROOM but not in a group, it cannot participate in sharing
            # (unless it's only sharing with itself, handled below)
            if len(shared_by_properties) > 1 or (
                len(shared_by_properties) == 1
                and shared_by_properties[0] != primary_property
            ):
                print(
                    f"    Validation FAILED: Primary property {primary_property.id} is a ROOM but not in a group, cannot share area."
                )
                raise serializers.ValidationError(
                    {
                        "shared_by": _(
                            "The primary property must belong to a group to share areas with others."
                        )
                    }
                )
            # If only shared_by=[primary_property], it's okay even without group.

        print(
            f"  Expected Group (from primary property {primary_property.id}): {expected_group}"
        )
        print("  Validation: Checking properties in shared_by list...")

        for prop in shared_by_properties:
            print(
                f"    Checking Property ID: {prop.id}, Name: {prop.name}, Category: {prop.property_category}, Group: {prop.group}, Landlord: {prop.landlord}"
            )

            # Rule 0: Check Ownership (ensure field didn't somehow get a property from another landlord)
            if prop.landlord != current_landlord:
                print(
                    f"    Validation FAILED: Property {prop.id} belongs to a different landlord."
                )
                raise serializers.ValidationError(
                    {
                        "shared_by": _(
                            "Cannot share with property '%(prop_name)s' as it belongs to another landlord."
                        )
                        % {"prop_name": prop.name}
                    }
                )

            # Rule 1: Must be ROOM
            if prop.property_category != Property.PropertyCategory.ROOM:
                print(f"    Validation FAILED: Property {prop.id} is not a ROOM.")
                raise serializers.ValidationError(
                    {
                        "shared_by": _(
                            "Only ROOM properties can share an area. Found: %(prop_name)s (%(prop_cat)s)."
                        )
                        % {
                            "prop_name": prop.name,
                            "prop_cat": prop.get_property_category_display(),
                        }
                    }
                )

            # Rule 2: Must belong to a group
            if not prop.group:
                print(
                    f"    Validation FAILED: Property {prop.id} does not belong to a group."
                )
                raise serializers.ValidationError(
                    {
                        "shared_by": _(
                            "Property '%(prop_name)s' must belong to a group to share an area."
                        )
                        % {"prop_name": prop.name}
                    }
                )

            # Rule 3: Must belong to the *same* group (establish target group on first iteration)
            if not first_prop_checked:
                target_group = prop.group
                first_prop_checked = True
                print(
                    f"    Established target_group: {target_group.id if target_group else 'None'}"
                )
                # Crucial Check: Ensure this established group matches the primary property's group (if primary is ROOM)
                if expected_group != target_group:
                    print(
                        f"    Validation FAILED: Group of first shared property ({target_group}) does not match primary property's group ({expected_group})."
                    )
                    raise serializers.ValidationError(
                        {
                            "shared_by": _(
                                "Properties listed for sharing must be in the same group as the primary property ('%(prop_name)s')."
                            )
                            % {"prop_name": primary_property.name}
                        }
                    )
            elif prop.group != target_group:
                print(
                    f"    Validation FAILED: Property {prop.id} group ({prop.group}) doesn't match target_group ({target_group})."
                )
                raise serializers.ValidationError(
                    {
                        "shared_by": _(
                            "All properties sharing an area must belong to the *same* group. Mismatch found for '%(prop_name)s'. Expected group: '%(group_name)s'."
                        )
                        % {"prop_name": prop.name, "group_name": target_group.name}
                    }
                )

        # --- Final Check for Primary Property (only if sharing occurs) ---
        if target_group:  # If sharing happened, target_group was set
            # Rule 4: The primary property itself must be valid for sharing
            if primary_property.property_category != Property.PropertyCategory.ROOM:
                print(
                    f"    Validation FAILED: Primary property {primary_property.id} is not a ROOM and cannot own shared areas."
                )
                raise serializers.ValidationError(
                    {
                        "shared_by": _(
                            "The primary property ('%(prop_name)s') must be a ROOM to participate in sharing."
                        )
                        % {"prop_name": primary_property.name}
                    }
                )
            # Rule 5: Primary property must match the target group (redundant check due to logic in loop, but safe)
            if primary_property.group != target_group:
                print(
                    f"    Validation FAILED: Primary property {primary_property.id} group ({primary_property.group}) doesn't match target_group ({target_group}) [Final Check]."
                )
                raise serializers.ValidationError(
                    {
                        "shared_by": _(
                            "The primary property ('%(prop_name)s') must belong to the same group ('%(group_name)s') as the properties sharing its area."
                        )
                        % {
                            "prop_name": primary_property.name,
                            "group_name": target_group.name,
                        }
                    }
                )

        print("  Validation: All sharing checks passed.")
        return data


# --- BasicPropertyGroupSerializer ---
class BasicPropertyGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = PropertyGroup
        fields = ["id", "name"]


# --- PropertySummaryForGroupSerializer ---
class PropertySummaryForGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = Property
        fields = ["id", "name", "address", "city", "property_category", "status"]


# --- PropertyGroupSerializer (For List View) ---
class PropertyGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = PropertyGroup
        fields = ["id", "landlord", "name", "description", "created_at", "updated_at"]
        read_only_fields = ["id", "landlord", "created_at", "updated_at"]


# --- PropertyGroupDetailSerializer ---
class PropertyGroupDetailSerializer(PropertyGroupSerializer):
    grouped_properties = PropertySummaryForGroupSerializer(many=True, read_only=True)
    # Use correct related_name
    shared_items = SharedInventoryItemSerializer(
        source="group_shared_inventory", many=True, read_only=True
    )

    class Meta(PropertyGroupSerializer.Meta):
        fields = PropertyGroupSerializer.Meta.fields + [
            "grouped_properties",
            "shared_items",
        ]
        read_only_fields = PropertyGroupSerializer.Meta.read_only_fields + [
            "grouped_properties",
            "shared_items",
        ]


# --- PropertySerializer (Detail View - Updated for Area/Inventory related_names) ---
class PropertySerializer(serializers.ModelSerializer):
    landlord_name = serializers.CharField(source="landlord.user.name", read_only=True)
    unit_type_display = serializers.CharField(
        source="get_unit_type_display", read_only=True, allow_null=True
    )
    room_type_display = serializers.CharField(
        source="get_room_type_display", read_only=True, allow_null=True
    )
    property_category_display = serializers.CharField(
        source="get_property_category_display", read_only=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    # Use updated related names and serializers
    additional_images = PropertyImageSerializer(
        source="property_images", many=True, read_only=True
    )  # Use model's related_name
    primary_areas = PropertyAreaSerializer(
        source="primary_area_associations", many=True, read_only=True
    )  # Areas owned by this property
    shared_areas = PropertyAreaSerializer(
        many=True, read_only=True
    )  # Areas this property shares (M2M)
    private_inventory_items = InventoryItemSerializer(
        source="inventory_items", many=True, read_only=True
    )  # Use model's related_name
    shared_inventory_items = SharedInventoryItemSerializer(
        many=True, read_only=True, allow_null=True
    )  # The field name matches the property name, so no source needed

    group = BasicPropertyGroupSerializer(read_only=True, allow_null=True)
    group_id = serializers.UUIDField(source="group.id", read_only=True, allow_null=True)
    group_name = serializers.CharField(
        source="group.name", read_only=True, allow_null=True
    )
    assign_group_id = serializers.PrimaryKeyRelatedField(
        queryset=PropertyGroup.objects.all(),  # narrowed per-landlord in __init__
        source="group",
        write_only=True,
        required=False,
        allow_null=True,
    )
    publish_blockers = serializers.SerializerMethodField()
    can_be_published = serializers.BooleanField(read_only=True)

    class Meta:
        model = Property
        fields = [
            "id",
            "landlord",
            "landlord_name",
            "name",
            "description",
            "address",
            "city",
            "province",
            "postal_code",
            "country",
            "property_category",
            "property_category_display",
            "primary_image",
            "status",
            "status_display",
            "unit_type",
            "unit_type_display",
            "bedrooms",
            "bathrooms",
            "max_occupancy",
            "square_footage",
            "room_type",
            "room_type_display",
            # Group Info
            "group",
            "group_id",
            "group_name",
            "assign_group_id",
            # Nested Data
            "additional_images",
            "primary_areas",  # Renamed from 'areas'
            "shared_areas",  # Added
            "private_inventory_items",
            "shared_inventory_items",
            # Timestamps
            "created_at",
            "updated_at",
            "is_publicly_visible",
            "public_slug",
            "asking_rent",
            "available_from",
            "is_furnished",  # read-only: derived from inventory
            "neighbourhood",
            "building_amenities",
            "default_bills_included",
            "latitude",
            "longitude",
            "publish_blockers",
            "can_be_published",
        ]
        read_only_fields = [
            "id",
            "landlord",
            "landlord_name",
            "unit_type_display",
            "room_type_display",
            "property_category_display",
            "status_display",
            "additional_images",
            "primary_areas",
            "shared_areas",  # Updated read-only fields
            "private_inventory_items",
            "shared_inventory_items",
            "group",
            "group_id",
            "group_name",
            "created_at",
            "updated_at",
            "public_slug",
            "is_furnished",
            "latitude",
            "longitude",
            "publish_blockers",
            "can_be_published",
        ]
        extra_kwargs = {
            "bathrooms": {"coerce_to_string": False, "allow_null": True},
            "bedrooms": {"allow_null": True},
            "max_occupancy": {"allow_null": True},
            "square_footage": {"allow_null": True},
            "unit_type": {"allow_null": True},
            "room_type": {"allow_null": True},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if (
            request
            and hasattr(request.user, "landlord_profile")
            and "assign_group_id" in self.fields
        ):
            self.fields["assign_group_id"].queryset = PropertyGroup.objects.filter(
                landlord=request.user.landlord_profile
            )

    def get_publish_blockers(self, obj):
        return obj.publish_blockers()

    def validate_assign_group_id(self, value):
        """`value` is a PropertyGroup instance (or None) resolved by DRF."""
        if value is None:
            return None

        request = self.context.get("request")
        landlord = getattr(getattr(request, "user", None), "landlord_profile", None)
        if landlord is None:
            raise serializers.ValidationError(
                _("Only landlords can assign property groups.")
            )
        if value.landlord_id != landlord.id:
            raise serializers.ValidationError(_("You don't own that property group."))
        return value

    def validate(self, data):
        """Validate category-specific requirements and group assignment rules."""
        instance = getattr(self, "instance", None)
        # Determine the category being set or the existing one if not provided
        property_category = data.get(
            "property_category", getattr(instance, "property_category", None)
        )
        # Determine the group being assigned, the existing one, or None
        # 'group' key in 'data' is populated by 'assign_group_id' source
        group = data.get("group", getattr(instance, "group", None))

        # --- Category Specific Validations ---
        if property_category == Property.PropertyCategory.COMPLETE_UNIT:
            # Required field for this category
            unit_type = data.get("unit_type", getattr(instance, "unit_type", None))
            if not unit_type and not instance:  # Required on create
                raise serializers.ValidationError(
                    {"unit_type": _("Unit type is required for Complete Units.")}
                )
            elif (
                "unit_type" in data and not data["unit_type"]
            ):  # Required if explicitly set to empty
                raise serializers.ValidationError(
                    {"unit_type": _("Unit type is required for Complete Units.")}
                )

            # Ensure related fields for ROOM are nullified if category changes
            if "room_type" in data and data.get("room_type") is not None:
                raise serializers.ValidationError(
                    {"room_type": _("Room type must be null for Complete Units.")}
                )
            elif (
                instance and instance.room_type is not None and "room_type" not in data
            ):
                data["room_type"] = None  # Nullify if not provided during update

            # Rule: Complete units cannot be in a group
            if group:
                raise serializers.ValidationError(
                    {
                        "assign_group_id": _(  # Error relates to the input field
                            "Complete units cannot be assigned to a group."
                        )
                    }
                )
            # If updating TO Complete Unit, ensure no shared areas exist
            if (
                instance
                and instance.pk
                and instance.property_category == Property.PropertyCategory.ROOM
            ):
                # Check if this property is listed in ANY area's shared_by field
                # where the area is not primarily owned by this property
                if (
                    PropertyArea.objects.filter(shared_by=instance)
                    .exclude(property=instance)
                    .exists()
                ):
                    raise serializers.ValidationError(
                        _(
                            "Cannot change to Complete Unit while it shares areas with other properties."
                        )
                    )
                # Check if any area primarily owned by this property is shared by others
                if (
                    PropertyArea.objects.filter(
                        property=instance, shared_by__isnull=False
                    )
                    .exclude(shared_by=instance)
                    .exists()
                ):
                    raise serializers.ValidationError(
                        _(
                            "Cannot change to Complete Unit while its primary areas are shared by others."
                        )
                    )

        elif property_category == Property.PropertyCategory.ROOM:
            # Required field for this category
            room_type = data.get("room_type", getattr(instance, "room_type", None))
            if not room_type and not instance:  # Required on create
                raise serializers.ValidationError(
                    {"room_type": _("Room type is required for Room rentals.")}
                )
            elif (
                "room_type" in data and not data["room_type"]
            ):  # Required if explicitly set to empty
                raise serializers.ValidationError(
                    {"room_type": _("Room type is required for Room rentals.")}
                )

            # Ensure related fields for COMPLETE_UNIT are nullified if category changes
            if "unit_type" in data and data.get("unit_type") is not None:
                raise serializers.ValidationError(
                    {"unit_type": _("Unit type must be null for Room rentals.")}
                )
            elif (
                instance and instance.unit_type is not None and "unit_type" not in data
            ):
                data["unit_type"] = None  # Nullify if not provided

        else:
            # This case should be prevented by the Enum field itself, but safeguard
            if (
                "property_category" in data
            ):  # Only raise if explicitly provided invalid category
                raise serializers.ValidationError(
                    {"property_category": _("Invalid property category specified.")}
                )

        # --- Landlord Consistency Check (already handled by validate_assign_group_id) ---
        # request = self.context.get("request")
        # if group and request and hasattr(request.user, "landlord_profile"):
        #     if group.landlord != request.user.landlord_profile:
        #         # This should be caught by validate_assign_group_id, but belt-and-suspenders
        #         raise serializers.ValidationError({"assign_group_id": _("Group does not belong to the current user.")})

        return data


# --- PropertyListSerializer (Updated get_area_summary) ---
class PropertyListSerializer(serializers.ModelSerializer):
    landlord_name = serializers.CharField(source="landlord.user.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    type_display = serializers.SerializerMethodField()
    group_name = serializers.CharField(
        source="group.name", read_only=True, allow_null=True
    )
    area_summary = serializers.SerializerMethodField()
    group_id = serializers.UUIDField(source="group.id", read_only=True, allow_null=True)
    property_category_display = serializers.CharField(
        source="get_property_category_display", read_only=True
    )

    class Meta:
        model = Property
        fields = [
            "id",
            "name",
            "address",
            "city",
            "property_category",
            "property_category_display",
            "type_display",
            "primary_image",
            "status",
            "status_display",
            "landlord_name",
            "group_name",
            "group_id",
            "area_summary",
        ]
        read_only_fields = [
            "id",
            "landlord_name",
            "status_display",
            "type_display",
            "group_name",
            "group_id",
            "area_summary",
            "property_category_display",
        ]

    def get_type_display(self, obj):
        # ... (remains the same)
        if obj.property_category == Property.PropertyCategory.COMPLETE_UNIT:
            return obj.get_unit_type_display()
        elif obj.property_category == Property.PropertyCategory.ROOM:
            return obj.get_room_type_display()
        return None

    def get_area_summary(self, obj):
        """Summarize both primary and shared areas."""
        summary = []
        # Prefetching should be done in the viewset for efficiency
        # e.g., .prefetch_related('primary_area_associations__shared_by', 'shared_areas__shared_by')

        processed_area_ids = set()

        # 1. Areas primarily associated with this property
        try:
            primary_areas = obj.primary_area_associations.all()
        except AttributeError:
            # Handle case where relation might not be loaded (e.g., during save)
            primary_areas = PropertyArea.objects.none()
            if obj.pk:  # If object exists, try fetching again (less efficient)
                try:
                    primary_areas = PropertyArea.objects.filter(
                        property=obj
                    ).prefetch_related("shared_by")
                except PropertyArea.DoesNotExist:
                    primary_areas = PropertyArea.objects.none()

        for area in primary_areas:
            if area.id in processed_area_ids:
                continue

            count_str = f"{area.count}x " if area.count > 1 else ""
            area_display = area.get_area_type_display() or area.area_type

            # Determine sharing status based on M2M field 'shared_by'
            try:
                # Check if shared_by is loaded, might need prefetch
                shared_by_qs = area.shared_by
                share_count = shared_by_qs.count()
                first_sharer = shared_by_qs.first() if share_count > 0 else None
            except Exception as e:
                # Handle potential errors accessing related manager if not prefetched well
                print(
                    f"Warning: Error accessing area.shared_by for area {area.id}: {e}"
                )
                share_count = 0
                first_sharer = None

            status = ""
            if share_count > 1:
                status = " (Shared)"
            elif share_count == 1 and first_sharer == obj:
                status = " (Private)"  # Explicitly shared only by self is private
            elif share_count == 0:
                status = " (Private)"  # Not shared by anyone
            elif share_count == 1 and first_sharer != obj:
                # This is unusual for a primary area but technically possible
                # if the primary property was removed from shared_by list later.
                status = (
                    f" (Shared by {first_sharer.name if first_sharer else 'Other'})"
                )

            summary.append(f"{count_str}{area_display}{status}")
            processed_area_ids.add(area.id)

        # 2. Areas this property shares (but doesn't own primarily)
        try:
            # Use updated related name
            shared_areas_access = obj.shared_areas.all()
        except AttributeError:
            shared_areas_access = PropertyArea.objects.none()
            if obj.pk:
                try:
                    # Less efficient fetch if not prefetched
                    shared_areas_access = PropertyArea.objects.filter(
                        shared_by=obj
                    ).exclude(property=obj)
                except PropertyArea.DoesNotExist:
                    shared_areas_access = PropertyArea.objects.none()

        for area in shared_areas_access:
            if area.id in processed_area_ids:
                continue  # Already processed as a primary area

            count_str = f"{area.count}x " if area.count > 1 else ""
            area_display = area.get_area_type_display() or area.area_type

            # All areas accessed via 'shared_areas' are inherently shared (by definition of the M2M)
            status = " (Shared)"

            summary.append(f"{count_str}{area_display}{status}")
            processed_area_ids.add(area.id)

        return ", ".join(summary) if summary else "No areas defined"
