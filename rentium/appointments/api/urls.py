from django.urls import include
from django.urls import path
from rest_framework.routers import DefaultRouter

from .public_views import public_email_events
from .public_views import public_property
from .public_views import public_viewing_request
from .public_views import public_viewing_respond
from .public_views import public_viewing_slots
from .public_views import public_viewing_status
from .views import AppointmentViewSet
from .views import AvailabilityWindowViewSet

app_name = "appointments"

router = DefaultRouter()
router.register("availability", AvailabilityWindowViewSet, basename="availability")
router.register("", AppointmentViewSet, basename="appointment")

# Authenticated landlord/tenant CRUD + confirm/decline + availability hours.
urlpatterns = [
    path("", include(router.urls)),
]

# Unauthenticated public booking funnel — kept as a SEPARATE list (not merged
# into urlpatterns) so it's obvious at a glance in config/urls.py that these
# routes carry no auth requirement.
#
# <str:property_id> (was <int:>) so we can hand out the un-guessable
# public_slug while old numeric links still resolve. Both paths run through
# Property.objects.public(), so consent is enforced either way.
public_urlpatterns = [
    path("properties/<str:property_id>/", public_property, name="public-property"),
    path(
        "properties/<str:property_id>/slots/",
        public_viewing_slots,
        name="public-viewing-slots",
    ),
    path("viewing-requests/", public_viewing_request, name="public-viewing-request"),
    path(
        "viewing-status/<uuid:token>/",
        public_viewing_status,
        name="public-viewing-status",
    ),
    path(
        "viewing-respond/<uuid:token>/",
        public_viewing_respond,
        name="public-viewing-respond",
    ),
    path("email-events/", public_email_events, name="public-email-events"),
]
