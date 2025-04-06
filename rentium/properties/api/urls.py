from django.urls import include
from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import PropertyViewSet

app_name = "properties"

router = DefaultRouter()
router.register("", PropertyViewSet, basename="property")

urlpatterns = [
    path("", include(router.urls)),
]
