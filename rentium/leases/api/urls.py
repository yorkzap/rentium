from django.urls import path, include
from rest_framework_nested import routers

from .views import (
    LeaseViewSet, LeaseTenantViewSet, LeaseDocumentViewSet,
    PaymentViewSet, PaymentReminderViewSet, lease_types_view
)

app_name = "leases_api"

# Create routers for leases
leases_router = routers.SimpleRouter()
leases_router.register("", LeaseViewSet, basename="lease-main")

# Register sub-resources under leases
leases_router.register("tenants", LeaseTenantViewSet, basename="lease-tenant")
leases_router.register("documents", LeaseDocumentViewSet, basename="lease-document")

# Register payment resources under leases
leases_router.register("payments", PaymentViewSet, basename="lease-payment")
payments_router = routers.SimpleRouter()
payments_router.register("payments/reminders", PaymentReminderViewSet, basename="payment-reminder")

# Optional: Create nested routes for specific lease IDs
lease_specific_router = routers.SimpleRouter()
lease_specific_router.register("", LeaseViewSet, basename="lease-specific")

lease_nested_router = routers.NestedSimpleRouter(lease_specific_router, "", lookup="lease")
lease_nested_router.register("tenants", LeaseTenantViewSet, basename="lease-specific-tenants")
lease_nested_router.register("documents", LeaseDocumentViewSet, basename="lease-specific-documents")
lease_nested_router.register("payments", PaymentViewSet, basename="lease-specific-payments")

# Optional: Create nested routes for specific payment IDs
payment_specific_router = routers.SimpleRouter()
payment_specific_router.register("payments", PaymentViewSet, basename="payment-specific")

payment_nested_router = routers.NestedSimpleRouter(payment_specific_router, "payments", lookup="payment")
payment_nested_router.register("reminders", PaymentReminderViewSet, basename="payment-specific-reminders")

urlpatterns = [
    # Critical: Place custom function views BEFORE the router includes!
    # Custom endpoint for lease types must come first
    path("types/", lease_types_view, name="lease-types"),
    
    # Include the lease router endpoints
    path("", include(leases_router.urls)),
    
    # Include payments router endpoints
    path("", include(payments_router.urls)),
    
    # Include nested specific-lease endpoints
    path("", include(lease_nested_router.urls)),
    
    # Include nested specific-payment endpoints
    path("", include(payment_nested_router.urls)),
]