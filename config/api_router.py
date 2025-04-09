from django.conf import settings
from rest_framework.routers import DefaultRouter
from rest_framework.routers import SimpleRouter

from rentium.properties.api.views import PropertyGroupViewSet

# Import viewsets
from rentium.properties.api.views import PropertyViewSet
from rentium.users.api.views import LandlordProfileViewSet
from rentium.users.api.views import TenantProfileViewSet
from rentium.users.api.views import UserViewSet

# Use DefaultRouter or SimpleRouter as before
router = DefaultRouter() if settings.DEBUG else SimpleRouter()

# Existing registrations
router.register("users", UserViewSet)
router.register("landlords", LandlordProfileViewSet, basename="landlord")
router.register("tenants", TenantProfileViewSet, basename="tenant")

# Register Property Group (Top Level)
router.register("property-groups", PropertyGroupViewSet, basename="property-group")

# Register Properties (Top Level)
router.register("properties", PropertyViewSet, basename="property")

app_name = "api"
urlpatterns = router.urls
