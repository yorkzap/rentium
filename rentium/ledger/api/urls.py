from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import import_views
from .views import (
    LedgerEntryViewSet,
    summary_view,
    tenant_statement_view,
    utility_bill_view,
)

app_name = "ledger_api"

router = DefaultRouter()
router.register("entries", LedgerEntryViewSet, basename="ledger-entry")

urlpatterns = [
    path("summary/", summary_view, name="summary"),
    path(
        "tenant-statement/",
        tenant_statement_view,
        name="tenant-statement",
    ),
    path("utility-bills/", utility_bill_view, name="utility-bills"),
    path("import/batches/", import_views.batches_view, name="import-batches"),
    path(
        "import/batches/<uuid:batch_id>/mapping/",
        import_views.apply_mapping_view,
        name="import-batch-mapping",
    ),
    path(
        "import/batches/<uuid:batch_id>/rows/",
        import_views.batch_rows_view,
        name="import-batch-rows",
    ),
    path(
        "import/batches/<uuid:batch_id>/rows/<uuid:row_id>/",
        import_views.row_detail_view,
        name="import-batch-row-detail",
    ),
    path(
        "import/batches/<uuid:batch_id>/commit/",
        import_views.commit_batch_view,
        name="import-batch-commit",
    ),
    path(
        "import/batches/<uuid:batch_id>/discard/",
        import_views.discard_batch_view,
        name="import-batch-discard",
    ),
    path("", include(router.urls)),
]
