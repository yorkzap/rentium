from django.db.models import Count
from django.db.models import Q
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import Property
from .serializers import PropertyListSerializer
from .serializers import PropertySerializer


class PropertyViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows properties to be viewed, created, edited, or deleted.

    Permissions:
    - Landlords can create, view, edit, and delete their own properties
    - Tenants currently have no access to property endpoints
    """

    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == "list":
            return PropertyListSerializer
        return PropertySerializer

    def get_queryset(self):
        user = self.request.user

        # If user is a landlord, show their properties
        if hasattr(user, "landlord_profile"):
            return Property.objects.filter(landlord=user.landlord_profile)

        # For now, return empty queryset for tenants
        return Property.objects.none()

    def perform_create(self, serializer):
        # Ensure the landlord is set to the current user's landlord profile
        if hasattr(self.request.user, "landlord_profile"):
            serializer.save(landlord=self.request.user.landlord_profile)
        else:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("Only landlords can create properties")

    @action(detail=False, methods=["get"])
    def dashboard_stats(self, request):
        """Return statistics for landlord dashboard"""
        if not hasattr(request.user, "landlord_profile"):
            return Response(
                {"detail": "Not authorized"}, status=status.HTTP_403_FORBIDDEN
            )

        landlord = request.user.landlord_profile
        properties = Property.objects.filter(landlord=landlord)

        # Get counts by category and status
        stats = {
            "total_properties": properties.count(),
            "complete_units": properties.filter(
                property_category=Property.PropertyCategory.COMPLETE_UNIT
            ).count(),
            "rooms": properties.filter(
                property_category=Property.PropertyCategory.ROOM
            ).count(),
            "available_properties": properties.filter(
                status=Property.PropertyStatus.AVAILABLE
            ).count(),
            "occupied_properties": properties.filter(
                status=Property.PropertyStatus.OCCUPIED
            ).count(),
            "maintenance_properties": properties.filter(
                status=Property.PropertyStatus.MAINTENANCE
            ).count(),
        }

        return Response(stats)

    @action(detail=False, methods=["get"])
    def available(self, request):
        """Return only available properties"""
        queryset = self.get_queryset().filter(status=Property.PropertyStatus.AVAILABLE)
        serializer = PropertyListSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def complete_units(self, request):
        """Return only complete unit properties"""
        queryset = self.get_queryset().filter(
            property_category=Property.PropertyCategory.COMPLETE_UNIT
        )
        serializer = PropertyListSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def rooms(self, request):
        """Return only room properties"""
        queryset = self.get_queryset().filter(
            property_category=Property.PropertyCategory.ROOM
        )
        serializer = PropertyListSerializer(queryset, many=True)
        return Response(serializer.data)
