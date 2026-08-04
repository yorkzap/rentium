from django.urls import include
from django.urls import path
from rest_framework_nested import routers

from .document_views import lease_document
from .document_views import lease_pdf
from .form_views import LeaseFormTemplateViewSet
from .form_views import LeaseFormViewSet
from .form_views import lease_activation_status
from .inspection_views import ConditionInspectionViewSet
from .moveout_views import MoveOutViewSet
from .moveout_views import lease_moveout_rules
from .public_form_views import public_form_decline
from .public_form_views import public_form_detail
from .public_form_views import public_form_page
from .public_form_views import public_form_pdf
from .public_form_views import public_form_sign
from .views import LeaseDocumentViewSet
from .views import LeaseTenantViewSet
from .views import LeaseViewSet
from .views import PaymentReminderViewSet
from .views import PaymentViewSet
from .views import RentAdjustmentViewSet
from .views import check_overlap_view
from .views import lease_types_view

app_name = "leases_api"

# --- Flat routes ---
#
# CRITICAL ORDERING NOTE: DRF routers try URL patterns in registration order,
# and LeaseViewSet's detail route (^(?P<pk>[^/.]+)/$) matches ANY single path
# segment — including literal words like "documents" or "payments". If
# LeaseViewSet is registered at the empty "" prefix BEFORE the sub-resources
# below, requests like GET /leases/documents/ get intercepted as "retrieve lease
# with pk=documents" and 404, never reaching LeaseDocumentViewSet. All non-""
# prefixes MUST be registered before "".
leases_router = routers.SimpleRouter()
leases_router.register("tenants", LeaseTenantViewSet, basename="lease-tenant")
leases_router.register("documents", LeaseDocumentViewSet, basename="lease-document")
leases_router.register("payments", PaymentViewSet, basename="lease-payment")
leases_router.register(
    "rent-adjustments", RentAdjustmentViewSet, basename="rent-adjustment"
)
leases_router.register(
    "form-templates", LeaseFormTemplateViewSet, basename="lease-form-template"
)
leases_router.register("forms", LeaseFormViewSet, basename="lease-form")
leases_router.register("inspections", ConditionInspectionViewSet, basename="inspection")
leases_router.register("moveouts", MoveOutViewSet, basename="lease-moveout")
leases_router.register(
    "payment-reminders", PaymentReminderViewSet, basename="payment-reminder"
)
leases_router.register("", LeaseViewSet, basename="lease-main")  # MUST STAY LAST

# --- Nested under a specific lease: /leases/{lease_pk}/tenants/, etc. ---
lease_specific_router = routers.SimpleRouter()
lease_specific_router.register("", LeaseViewSet, basename="lease-specific")

lease_nested_router = routers.NestedSimpleRouter(
    lease_specific_router, "", lookup="lease"
)
lease_nested_router.register(
    "tenants", LeaseTenantViewSet, basename="lease-specific-tenants"
)
lease_nested_router.register(
    "documents", LeaseDocumentViewSet, basename="lease-specific-documents"
)
lease_nested_router.register(
    "payments", PaymentViewSet, basename="lease-specific-payments"
)

# --- Nested under a specific lease tenant ---
lease_tenant_specific_router = routers.SimpleRouter()
lease_tenant_specific_router.register(
    "tenants", LeaseTenantViewSet, basename="lease-tenant-specific"
)
lease_tenant_nested_router = routers.NestedSimpleRouter(
    lease_tenant_specific_router, "tenants", lookup="lease_tenant"
)
lease_tenant_nested_router.register(
    "rent-adjustments", RentAdjustmentViewSet, basename="lease-tenant-rent-adjustments"
)

# --- Nested under a specific payment ---
payment_specific_router = routers.SimpleRouter()
payment_specific_router.register(
    "payments", PaymentViewSet, basename="payment-specific"
)
payment_nested_router = routers.NestedSimpleRouter(
    payment_specific_router, "payments", lookup="payment"
)
payment_nested_router.register(
    "reminders", PaymentReminderViewSet, basename="payment-specific-reminders"
)

urlpatterns = [
    # Custom function views must come before the router includes so their fixed
    # path segments ("types/", "<id>/pdf/") don't get swallowed by the "" lease
    # detail route.
    path("types/", lease_types_view, name="lease-types"),
    path("check-overlap/", check_overlap_view, name="lease-check-overlap"),
    path(
        "<uuid:lease_id>/moveout-rules/",
        lease_moveout_rules,
        name="lease-moveout-rules",
    ),
    # --- The lease AS A DOCUMENT (leases/documents.py) ---
    # `pdf/` keeps its original URL, so the frontend's existing download button
    # needs no change — but it now resolves here instead of to the @action that
    # used to live on LeaseViewSet (see the deletion note below).
    path("<uuid:lease_id>/document/", lease_document, name="lease-document-render"),
    path("<uuid:lease_id>/pdf/", lease_pdf, name="lease-pdf"),
    # "Why hasn't this lease activated?" — including any form pack it waits on.
    path(
        "<uuid:lease_id>/activation-status/",
        lease_activation_status,
        name="lease-activation-status",
    ),
    path("", include(leases_router.urls)),
    path("", include(lease_nested_router.urls)),
    path("", include(lease_tenant_nested_router.urls)),
    path("", include(payment_nested_router.urls)),
]

# --- PUBLIC, UNAUTHENTICATED (mounted under /api/public/ by config/urls.py) ---
#
# Signing an attached form does not require a Rentium account. The lease itself
# still does — an account is worth the friction for the document a tenant needs
# ongoing access to. A guarantor putting their name on one page is not.
# `sign_token` is a single-slot, single-use, expiring capability; see
# leases/api/public_form_views.py for the full reasoning.
public_urlpatterns = [
    path(
        "lease-forms/<uuid:token>/",
        public_form_detail,
        name="public-lease-form",
    ),
    path(
        "lease-forms/<uuid:token>/pdf/",
        public_form_pdf,
        name="public-lease-form-pdf",
    ),
    path(
        "lease-forms/<uuid:token>/page/<int:page>/",
        public_form_page,
        name="public-lease-form-page",
    ),
    path(
        "lease-forms/<uuid:token>/sign/",
        public_form_sign,
        name="public-lease-form-sign",
    ),
    path(
        "lease-forms/<uuid:token>/decline/",
        public_form_decline,
        name="public-lease-form-decline",
    ),
]

# --- Endpoint summary this produces (all under /api/leases/...) ---
#
# GET/POST         /leases/
# GET/PUT/PATCH/DELETE /leases/{id}/
# GET  /leases/{id}/document/              <- NEW: the rendered agreement (JSON)
# GET  /leases/{id}/pdf/                   <- now rendered from the SAME source
# GET  /leases/{id}/calculate_bill_share/
# GET  /leases/{id}/all_bill_shares/
# GET  /leases/bill_providers/
# POST /leases/{id}/create_utility_payment/
# POST /leases/{id}/terminate/
# POST /leases/{id}/renew/
# GET  /leases/available_tenants/
# POST /leases/{id}/landlord_sign/
# POST /leases/{id}/preview-split/
#
# GET/POST         /leases/tenants/
# POST /leases/tenants/{id}/sign/ | /decline/ | /claim/ | /activate-account/
# POST /leases/tenants/{id}/resend_invite/
#
# GET/POST         /leases/inspections/  ... (see inspection_views.py)
# GET/POST         /leases/moveouts/?lease=<id>
# GET  /leases/{lease_id}/moveout-rules/
