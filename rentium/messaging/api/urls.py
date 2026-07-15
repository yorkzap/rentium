from rest_framework.routers import DefaultRouter

from .views import ConversationViewSet

app_name = "messaging_api"

router = DefaultRouter()
router.register("conversations", ConversationViewSet, basename="conversation")

urlpatterns = router.urls
