from django.conf import settings
from rest_framework.routers import DefaultRouter, SimpleRouter

from rentium.users.api.views import UserViewSet, LandlordProfileViewSet, TenantProfileViewSet

router = DefaultRouter() if settings.DEBUG else SimpleRouter()

router.register("users", UserViewSet)
router.register("landlords", LandlordProfileViewSet, basename="landlord")
router.register("tenants", TenantProfileViewSet, basename="tenant")

app_name = "api"
urlpatterns = router.urls
