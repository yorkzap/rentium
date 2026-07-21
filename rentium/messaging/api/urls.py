from django.urls import path
from rest_framework.routers import DefaultRouter

from .public_views import public_thread, public_thread_send
from .views import ConversationViewSet

app_name = "messaging_api"

router = DefaultRouter()
router.register("conversations", ConversationViewSet, basename="conversation")

urlpatterns = router.urls

# NO AUTHENTICATION — the prospect's tokenized chat (see public_views.py).
public_urlpatterns = [
    path("chat/<uuid:token>/", public_thread, name="public-chat"),
    path("chat/<uuid:token>/send/", public_thread_send, name="public-chat-send"),
]
