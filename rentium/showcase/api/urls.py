"""
Two url lists, deliberately kept apart.

`public_urlpatterns` carries NO authentication. `urlpatterns` requires a
landlord. Keeping them in separate names — rather than one list with mixed
permissions — means config/urls.py has to mount them separately, which means the
project's root URLconf makes it obvious at a glance which routes are open to the
internet. A reviewer shouldn't have to open a viewset to find that out.
"""

from django.urls import include
from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import InquiryViewSet
from .views import ShowcaseSettingsViewSet
from .views import address_search
from .views import public_cities_index
from .views import public_city
from .views import public_inquiry
from .views import public_property_detail
from .views import public_showcase
from .views import sitemap_data

app_name = "showcase"

# --- Unauthenticated. Every view here declares AllowAny explicitly + throttles. ---
public_urlpatterns = [
    path("cities/", public_cities_index, name="cities-index"),
    path("cities/<str:province>/<str:city>/", public_city, name="city"),
    path("l/<slug:slug>/", public_showcase, name="showcase"),
    path("listings/<slug:slug>/", public_property_detail, name="property"),
    path("inquiries/", public_inquiry, name="inquiry"),
    path("sitemap-data/", sitemap_data, name="sitemap-data"),
]

# --- Landlord-authenticated. ---
router = DefaultRouter()
router.register("inquiries", InquiryViewSet, basename="landlord-inquiry")
router.register("settings", ShowcaseSettingsViewSet, basename="showcase-settings")

urlpatterns = [
    # Address autocomplete, proxied through us so the Geoapify key never reaches
    # a browser. MUST come before the router include: DefaultRouter's generated
    # detail routes match a single path segment, and "address-search" is a single
    # path segment — registered after the router, it would be swallowed as a
    # lookup for an inquiry with pk="address-search". This is the same collision
    # already documented at length in leases/api/urls.py.
    path("address-search/", address_search, name="address-search"),
    path("", include(router.urls)),
]
