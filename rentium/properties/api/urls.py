from django.urls import include
from django.urls import path
from rest_framework.routers import DefaultRouter

from .group_area_views import group_common_area_detail
from .group_area_views import group_common_areas
from .views import PropertyGroupViewSet
from .views import PropertyHierarchyView
from .views import PropertyUnitViewSet
from .views import PropertyViewSet

app_name = "properties"

router = DefaultRouter()
# "groups" must be registered BEFORE the root "" registration below —
# PropertyViewSet's detail route (^(?P<pk>[^/.]+)/$) matches any single
# path segment, including the literal word "groups", if it's registered
# first. Same class of bug already documented in leases/api/urls.py.
router.register("groups", PropertyGroupViewSet, basename="property-group")
# Same ordering rule as "groups": anything with a literal first segment must be
# registered before the catch-all "" below, or PropertyViewSet's detail route
# swallows it.
router.register("units", PropertyUnitViewSet, basename="property-unit")
router.register("hierarchy", PropertyHierarchyView, basename="property-hierarchy")
router.register("", PropertyViewSet, basename="property")

urlpatterns = [
    path(
        "property-groups/<uuid:group_id>/common-areas/",
        group_common_areas,
        name="group-common-areas",
    ),
    path(
        "property-groups/<uuid:group_id>/common-areas/<int:area_id>/",
        group_common_area_detail,
        name="group-common-area-detail",
    ),
    path("", include(router.urls)),
]
