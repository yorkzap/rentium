from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import WorkOrderViewSet, areas_view

app_name = "maintenance_api"

router = DefaultRouter()
router.register("work-orders", WorkOrderViewSet, basename="work-order")

urlpatterns = [
    path("areas/", areas_view, name="areas"),
    path("", include(router.urls)),
]
