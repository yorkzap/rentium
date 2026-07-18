from django.urls import path

from . import views

app_name = "rama"

urlpatterns = [
    path("chat/", views.chat_view, name="chat"),
    path("config/", views.config_view, name="config"),
    path("settings/", views.settings_view, name="settings"),
    path("state-of-the-union/", views.union_view, name="union"),
]
