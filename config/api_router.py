from django.conf import settings
from rest_framework.routers import DefaultRouter
from rest_framework.routers import SimpleRouter

from rentium.users.api.views import LandlordProfileViewSet
from rentium.users.api.views import TenantProfileViewSet
from rentium.users.api.views import UserViewSet

# Use DefaultRouter if in DEBUG for browsable API
router = DefaultRouter() if settings.DEBUG else SimpleRouter()

# Only genuinely root-level, cross-app resources live here. Domain-specific
# viewsets (leases, properties, property groups, etc.) are routed from
# their own app's api/urls.py under their own /api/<app>/ prefix instead —
# registering the same viewset in two places causes silent URL shadowing
# (see the properties/leases history in this project for why).
router.register("users", UserViewSet)
router.register("landlords", LandlordProfileViewSet, basename="landlord")
router.register("tenants", TenantProfileViewSet, basename="tenant")

app_name = "api"
urlpatterns = router.urls
