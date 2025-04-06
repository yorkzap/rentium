from django.urls import include
from django.urls import path

app_name = "properties"

urlpatterns = [
    path("api/", include("rentium.properties.api.urls", namespace="api")),
]
