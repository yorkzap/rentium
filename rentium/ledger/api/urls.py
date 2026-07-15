from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import LedgerEntryViewSet, summary_view, utility_bill_view

app_name = "ledger_api"

router = DefaultRouter()
router.register("entries", LedgerEntryViewSet, basename="ledger-entry")

urlpatterns = [
    path("summary/", summary_view, name="summary"),
    path("utility-bills/", utility_bill_view, name="utility-bills"),
    path("", include(router.urls)),
]
