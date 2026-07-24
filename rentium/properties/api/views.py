# rentium/properties/api/views.py

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models  # Import models for Q objects
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import BasePermission
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

# from rentium.users.models import LandlordProfile # Not directly used here
from ..models import InventoryItem
from ..models import Property
from ..models import PropertyArea
from ..models import PropertyGroup
from ..models import PropertyImage
from ..models import SharedInventoryItem
from .serializers import InventoryItemSerializer
from .serializers import PropertyAreaSerializer
from .serializers import PropertyGroupDetailSerializer
from .serializers import PropertyGroupSerializer
from .serializers import PropertyImageSerializer
from .serializers import PropertyListSerializer
from .serializers import PropertySerializer
from .serializers import PropertySummaryForGroupSerializer
from .serializers import SharedInventoryItemSerializer


# --- IsLandlordOwner Permission ---
class IsLandlordOwner(BasePermission):
    """
    Checks if the request.user's landlord profile owns the object.
    Handles different object types.
    """

    def has_object_permission(self, request, view, obj):
        if not hasattr(request.user, "landlord_profile"):
            return False

        owner_profile = None
        # Determine owner based on object type
        if isinstance(obj, (Property, PropertyGroup)):
            owner_profile = obj.landlord
        elif isinstance(obj, SharedInventoryItem):
            owner_profile = obj.group.landlord
        elif isinstance(obj, PropertyArea):
            owner_profile = obj.property.landlord  # Check primary property owner
            # More complex check needed if editing shared_by involving other landlords?
            # Let serializer/view validation handle cross-landlord sharing rules.
        elif isinstance(obj, InventoryItem):
            owner_profile = obj.property.landlord
        elif isinstance(obj, PropertyImage):
            owner_profile = obj.property.landlord

        if owner_profile == request.user.landlord_profile:
            return True

        # A co-landlord granted access to this property (or its group) may also
        # act on it — mirrors accessible_properties() used by the list view.
        from rentium.users.access import accessible_properties

        prop = None
        group = None
        if isinstance(obj, Property):
            prop = obj
        elif isinstance(obj, PropertyGroup):
            group = obj
        elif isinstance(obj, SharedInventoryItem):
            group = obj.group
        elif isinstance(obj, (PropertyArea, InventoryItem, PropertyImage)):
            prop = getattr(obj, "property", None)
        acc = accessible_properties(request.user)
        if prop is not None:
            return acc.filter(pk=prop.pk).exists()
        if group is not None:
            return acc.filter(group=group).exists()
        return False


# --- PropertyViewSet ---
class PropertyViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsLandlordOwner]

    def get_serializer_class(self):
        if self.action == "list":
            return PropertyListSerializer
        # Use the detail serializer for retrieve, create, update, partial_update
        return PropertySerializer

    def get_queryset(self):
        from rentium.users.access import accessible_properties

        user = self.request.user
        accessible = accessible_properties(user)
        if accessible.exists() or hasattr(user, "landlord_profile"):
            # own properties + any granted to this user as a co-landlord
            return (
                accessible
                .select_related(
                    "group", "landlord__user"
                )  # Select related landlord user for name
                .prefetch_related(
                    # Prefetch areas owned by the property and the properties sharing them
                    models.Prefetch(
                        "primary_area_associations",  # Use new related_name
                        queryset=PropertyArea.objects.prefetch_related("shared_by"),
                    ),
                    # Prefetch areas shared BY this property (M2M reverse) and their primary owner
                    models.Prefetch(
                        "shared_areas",  # Use new related_name
                        queryset=PropertyArea.objects.select_related(
                            "property"
                        ).prefetch_related("shared_by"),
                    ),
                    # Prefetch inventory and images using new related_names
                    "inventory_items",
                    "property_images",
                    # Prefetch shared inventory via the group
                    "group__group_shared_inventory",
                )
            )
        return Property.objects.none()

    def perform_create(self, serializer):
        if not hasattr(self.request.user, "landlord_profile"):
            raise PermissionDenied("Only Landlords can create properties.")
        serializer.save(landlord=self.request.user.landlord_profile)

    @action(detail=True, methods=["post"])
    def invite_co_landlord(self, request, pk=None):
        """Invite a co-landlord scoped to THIS property (and its group). Every
        future lease on it names them as a co-signing landlord; they can manage
        and message its existing tenants. Owner only."""
        from rentium.leases.services import grant_co_landlord

        prop = self.get_object()
        if not hasattr(request.user, "landlord_profile") or (
            prop.landlord_id != request.user.landlord_profile.id
        ):
            raise PermissionDenied("Only the property owner can invite co-landlords.")
        name = (request.data.get("name") or "").strip()
        email = (request.data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            raise ValidationError("A valid email is required.")
        if email == getattr(request.user, "email", "").lower():
            raise ValidationError("That's your own account.")
        _member, _created, emailed = grant_co_landlord(
            prop.landlord, name=name, email=email, scope_property=prop
        )
        return Response({"invited": True, "emailed": emailed, "email": email})

    def get_serializer_context(self):
        """Add request and property instance (if applicable) to context."""
        context = super().get_serializer_context()
        context["request"] = self.request
        # Pass property instance for area/inventory creation/validation context
        # Use self.lookup_url_kwarg or default 'pk'
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        if lookup_url_kwarg in self.kwargs:
            try:
                # Get object without triggering full permission checks again if needed
                context["property"] = self.get_object()
            except (AssertionError, NotFound, PermissionDenied):
                # If object not found or not permitted here, let standard DRF flow handle it
                pass
        return context

    # --- Area Actions (Using new PropertyAreaSerializer) ---
    @action(
        detail=True,
        methods=["get", "post"],
        url_path="areas",  # Endpoint to manage areas primarily associated with this property
        serializer_class=PropertyAreaSerializer,
    )
    def areas(self, request, pk=None):
        """List or Create areas primarily associated with this Property."""
        property_obj = self.get_object()  # Ensures property exists and user owns it
        context = self.get_serializer_context()  # Get context including property

        if request.method == "GET":
            # Use the updated related_name
            queryset = property_obj.primary_area_associations.prefetch_related(
                "shared_by"
            )
            serializer = PropertyAreaSerializer(queryset, many=True, context=context)
            return Response(serializer.data)

        elif request.method == "POST":
            serializer = PropertyAreaSerializer(data=request.data, context=context)
            if serializer.is_valid():
                # Serializer validation already checked group/landlord consistency
                # Save the area, primarily linking it to property_obj
                serializer.save(property=property_obj)
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=True,
        methods=["get", "put", "patch", "delete"],
        url_path="areas/(?P<area_pk>[^/.]+)",  # Endpoint for specific area detail
        serializer_class=PropertyAreaSerializer,
    )
    def area_detail(self, request, pk=None, area_pk=None):
        """Retrieve, Update, or Delete a specific PropertyArea."""
        property_obj = (
            self.get_object()
        )  # Context property (might not be the primary owner)
        # Fetch the area ensuring it's somehow linked (primarily or via sharing?)
        # For simplicity, let's fetch based on PK and let serializer validate context.
        # More robust: Ensure area is either primarily owned OR shared by property_obj?
        area = get_object_or_404(PropertyArea, pk=area_pk)
        # Check if the user owns the *primary* property of the area being modified
        self.check_object_permissions(request, area)
        context = self.get_serializer_context()  # Includes request
        context["property"] = (
            area.property
        )  # Ensure context property is the primary owner for validation

        if request.method == "GET":
            serializer = PropertyAreaSerializer(area, context=context)
            return Response(serializer.data)

        elif request.method in ["PUT", "PATCH"]:
            serializer = PropertyAreaSerializer(
                area,
                data=request.data,
                partial=(request.method == "PATCH"),
                context=context,  # Pass context for validation
            )
            if serializer.is_valid():
                # Validation includes checking M2M group consistency
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        elif request.method == "DELETE":
            # Add logic: What happens when deleting a shared area?
            # Option 1: Delete it entirely (simplest).
            # Option 2: Only remove the 'property_obj' from shared_by? (More complex)
            # Let's go with Option 1 for now.
            area.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

    # --- PRIVATE Inventory Actions (Using updated related_name) ---
    @action(
        detail=True,
        methods=["get", "post"],
        url_path="inventory",
        serializer_class=InventoryItemSerializer,
    )
    def inventory(self, request, pk=None):
        property_obj = self.get_object()
        context = self.get_serializer_context()
        if request.method == "GET":
            queryset = property_obj.inventory_items.all()  # Use updated related_name
            serializer = InventoryItemSerializer(queryset, many=True, context=context)
            return Response(serializer.data)
        elif request.method == "POST":
            serializer = InventoryItemSerializer(data=request.data, context=context)
            if serializer.is_valid():
                serializer.save(property=property_obj)
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=True,
        methods=["get", "put", "patch", "delete"],
        url_path="inventory/(?P<item_pk>[^/.]+)",
        serializer_class=InventoryItemSerializer,
    )
    def inventory_detail(self, request, pk=None, item_pk=None):
        property_obj = self.get_object()
        # Use updated related_name for filtering
        item = get_object_or_404(InventoryItem, pk=item_pk, property=property_obj)
        # self.check_object_permissions(request, item) # Already covered by property check + query filter
        context = self.get_serializer_context()
        if request.method == "GET":
            serializer = InventoryItemSerializer(item, context=context)
            return Response(serializer.data)
        elif request.method in ["PUT", "PATCH"]:
            serializer = InventoryItemSerializer(
                item,
                data=request.data,
                partial=(request.method == "PATCH"),
                context=context,
            )
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        elif request.method == "DELETE":
            item.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

    # --- Image Actions (Assume PropertyImageViewSet exists or handle here) ---
    # Add actions for listing, creating, updating, deleting PropertyImages if needed
    # Example:
    @action(
        detail=True,
        methods=["get", "post"],
        url_path="images",
        serializer_class=PropertyImageSerializer,
    )
    def images(self, request, pk=None):
        property_obj = self.get_object()
        context = self.get_serializer_context()
        if request.method == "GET":
            # Use updated related_name
            queryset = property_obj.property_images.all()
            serializer = PropertyImageSerializer(queryset, many=True, context=context)
            return Response(serializer.data)
        elif request.method == "POST":
            serializer = PropertyImageSerializer(data=request.data, context=context)
            if serializer.is_valid():
                # Ensure image file is handled correctly (requires multipart/form-data)
                serializer.save(property=property_obj)
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=True,
        methods=["get", "put", "patch", "delete"],
        url_path="images/(?P<image_pk>[^/.]+)",
        serializer_class=PropertyImageSerializer,
    )
    def image_detail(self, request, pk=None, image_pk=None):
        property_obj = self.get_object()
        # Use updated related_name
        image = get_object_or_404(PropertyImage, pk=image_pk, property=property_obj)
        # self.check_object_permissions(request, image) # Covered by property check + filter
        context = self.get_serializer_context()

        if request.method == "GET":
            serializer = PropertyImageSerializer(image, context=context)
            return Response(serializer.data)
        elif request.method in ["PUT", "PATCH"]:
            serializer = PropertyImageSerializer(
                image,
                data=request.data,
                partial=request.method == "PATCH",
                context=context,
            )
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        elif request.method == "DELETE":
            image.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)


# --- PropertyGroupViewSet (Updated related_names) ---
class PropertyGroupViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsLandlordOwner]

    def get_serializer_class(self):
        if self.action in ["retrieve", "update", "partial_update"]:
            return PropertyGroupDetailSerializer
        return PropertyGroupSerializer

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, "landlord_profile"):
            from django.db.models import Q

            from rentium.users.access import accessible_properties, scope_q

            # Own/portfolio groups + any group that contains a property this
            # co-landlord can access (so a room-scoped grant sees its unit).
            grp_q = scope_q(user, landlord_field="landlord") | Q(
                grouped_properties__in=accessible_properties(user)
            )
            return PropertyGroup.objects.filter(grp_q).distinct().prefetch_related(
                "grouped_properties",  # Properties primarily in this group
                "group_shared_inventory",  # Shared inventory for this group
                # Maybe prefetch areas shared within the group? Complex query.
                # models.Prefetch(
                #    'grouped_properties__shared_areas',
                #    queryset=PropertyArea.objects.filter(...) # Filter areas shared only by properties in THIS group
                # )
            )
        return PropertyGroup.objects.none()

    def perform_create(self, serializer):
        if not hasattr(self.request.user, "landlord_profile"):
            raise PermissionDenied("Landlords only.")
        serializer.save(landlord=self.request.user.landlord_profile)

    def get_serializer_context(self):
        """Add request and group instance (if applicable) to context."""
        context = super().get_serializer_context()
        context["request"] = self.request
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        if lookup_url_kwarg in self.kwargs:
            try:
                context["group"] = self.get_object()
            except (AssertionError, NotFound, PermissionDenied):
                pass
        return context

    # --- Property add/remove actions (Keep logic, check constraints) ---
    @action(detail=True, methods=["post"], url_path="add-property")
    def add_property(self, request, pk=None):
        group = self.get_object()  # Checks ownership
        property_id = request.data.get("property_id")
        if not property_id:
            raise ValidationError("Missing 'property_id'.")

        try:
            # Ensure property exists and is owned by the user
            prop = Property.objects.get(
                pk=property_id, landlord=request.user.landlord_profile
            )
        except Property.DoesNotExist:
            raise NotFound("Property not found or not owned by you.")

        # Validation checks
        if prop.property_category != Property.PropertyCategory.ROOM:
            raise ValidationError("Only Room properties can be added to a group.")
        if prop.group == group:
            return Response(
                {"message": "Property already in this group."},
                status=status.HTTP_200_OK,
            )
        if prop.group is not None:
            raise ValidationError(
                f"Property '{prop.name}' is already assigned to group '{prop.group.name}'. Remove it first."
            )

        try:
            from rentium.properties.services import assign_room_to_group

            prop = assign_room_to_group(prop, group)
        except DjangoValidationError as e:
            raise ValidationError(
                serializers.as_serializer_error(e)
            )  # Convert to DRF error

        # Return summary of the added property
        serializer = PropertySummaryForGroupSerializer(
            prop, context={"request": request}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="remove-property")
    def remove_property(self, request, pk=None):
        group = self.get_object()  # Checks ownership
        property_id = request.data.get("property_id")
        if not property_id:
            raise ValidationError("Missing 'property_id'.")

        try:
            # Ensure property is owned and currently in this group
            prop = Property.objects.get(
                pk=property_id, landlord=request.user.landlord_profile, group=group
            )
        except Property.DoesNotExist:
            raise NotFound("Property not found in this group or not owned by you.")

        try:
            from rentium.properties.services import assign_room_to_group

            assign_room_to_group(prop, None)
        except DjangoValidationError as e:
            raise ValidationError(serializers.as_serializer_error(e))

        return Response(
            {"message": "Property removed from group."}, status=status.HTTP_200_OK
        )

    # --- SHARED Inventory Actions (Using updated related_name) ---
    @action(
        detail=True,
        methods=["get", "post"],
        url_path="shared-inventory",
        serializer_class=SharedInventoryItemSerializer,
    )
    def shared_inventory(self, request, pk=None):
        group = self.get_object()  # Ownership checked
        context = self.get_serializer_context()  # Includes group context
        if request.method == "GET":
            queryset = group.group_shared_inventory.all()  # Use updated related_name
            serializer = SharedInventoryItemSerializer(
                queryset, many=True, context=context
            )
            return Response(serializer.data)
        elif request.method == "POST":
            serializer = SharedInventoryItemSerializer(
                data=request.data, context=context
            )
            if serializer.is_valid():
                serializer.save(group=group)  # Associate with this group
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=True,
        methods=["get", "put", "patch", "delete"],
        url_path="shared-inventory/(?P<item_pk>[^/.]+)",
        serializer_class=SharedInventoryItemSerializer,
    )
    def shared_inventory_detail(self, request, pk=None, item_pk=None):
        group = self.get_object()  # Group ownership checked
        # Use updated related_name for filtering
        item = get_object_or_404(SharedInventoryItem, pk=item_pk, group=group)
        # self.check_object_permissions(request, item) # Covered by group check + filter
        context = self.get_serializer_context()

        if request.method == "GET":
            serializer = SharedInventoryItemSerializer(item, context=context)
            return Response(serializer.data)
        elif request.method in ["PUT", "PATCH"]:
            serializer = SharedInventoryItemSerializer(
                item,
                data=request.data,
                partial=(request.method == "PATCH"),
                context=context,
            )
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        elif request.method == "DELETE":
            item.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
