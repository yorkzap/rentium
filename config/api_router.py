# config/api_router.py
from django.conf import settings
from rest_framework.routers import DefaultRouter, SimpleRouter

# Core user/property routers
from rentium.properties.api.views import PropertyGroupViewSet, PropertyViewSet
from rentium.users.api.views import LandlordProfileViewSet, TenantProfileViewSet, UserViewSet

# Lease related viewsets
from rentium.leases.api.views import (
    LeaseViewSet, PaymentViewSet
)

# Use DefaultRouter if in DEBUG for browsable API
router = DefaultRouter() if settings.DEBUG else SimpleRouter()

# User and property routes
router.register("users", UserViewSet)
router.register("landlords", LandlordProfileViewSet, basename="landlord")
router.register("tenants", TenantProfileViewSet, basename="tenant")
router.register("property-groups", PropertyGroupViewSet, basename="property-group")
router.register("properties", PropertyViewSet, basename="property")
router.register("leases", LeaseViewSet, basename="lease")

app_name = "api"
urlpatterns = router.urls