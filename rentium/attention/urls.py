from django.urls import path

from .views import attention_view

app_name = "attention"

urlpatterns = [
    path("", attention_view, name="attention"),
]
