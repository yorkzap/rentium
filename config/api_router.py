from django.conf import settings
from rest_framework.routers import DefaultRouter
from rest_framework.routers import SimpleRouter

from rentium.users.api.views import LandlordProfileViewSet
from rentium.users.api.views import TenantProfileViewSet
from rentium.users.api.views import UserViewSet

router = DefaultRouter() if settings.DEBUG else SimpleRouter()

router.register("user", UserViewSet)
router.register("landlords", LandlordProfileViewSet, basename="landlord")
router.register("tenants", TenantProfileViewSet, basename="tenant")

app_name = "api"
urlpatterns = router.urls
