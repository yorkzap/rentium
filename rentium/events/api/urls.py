from rest_framework.routers import DefaultRouter

from .views import NotificationViewSet

app_name = "events_api"

router = DefaultRouter()
router.register("notifications", NotificationViewSet, basename="notification")

urlpatterns = router.urls
