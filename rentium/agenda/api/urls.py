from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AgendaEventViewSet, agenda_feed

app_name = "agenda_api"

router = DefaultRouter()
router.register("events", AgendaEventViewSet, basename="agenda-event")

urlpatterns = [
    path("", agenda_feed, name="feed"),
] + router.urls
