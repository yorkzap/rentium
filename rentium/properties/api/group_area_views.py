"""
Common-area management for property groups.

WIRING (add to your urls where /property-groups/ routes live):

    from rentium.properties.api.group_area_views import (
        group_common_areas, group_common_area_detail,
    )
    path("property-groups/<uuid:group_id>/common-areas/", group_common_areas),
    path("property-groups/<uuid:group_id>/common-areas/<int:area_id>/", group_common_area_detail),

What counts as a GROUP COMMON AREA here: a PropertyArea whose primary
property is a room in the group AND whose shared_by includes 2+ rooms —
i.e. "Shared (With Group)" in the property-creation form's terms. Areas
private to one room (ensuite bathroom, in-room closet) never appear here;
they stay managed on the individual property.

Endpoints:
  GET    .../common-areas/            -> list group common areas
  POST   .../common-areas/            -> create one, shared by ALL rooms in
                                          the group {area_type, count?,
                                          description?, shared_with_landlord?}
  PATCH  .../common-areas/<area_id>/  -> update count / description /
                                          shared_with_landlord
  DELETE .../common-areas/<area_id>/  -> remove the common area

The shared_with_landlord flag is the whole point: it is what flips leases
on these rooms into the RTA s.4(c) exemption (landlord shares kitchen/
common areas -> the Act doesn't apply; the lease's own notice terms do).
See leases/tenancy_rules.py. New leases derive their shared-with-landlord
clause from it automatically; already-signed leases keep the clause they
were signed with (rules also re-check the live flag, so move-out rules
stay current either way).
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rentium.properties.models import Property, PropertyArea, PropertyGroup
from rentium.properties.services import create_group_common_area
from rentium.properties.services import group_common_areas as common_area_queryset
from rentium.properties.services import update_group_common_area


def _landlord(request):
    if not hasattr(request.user, "landlord_profile"):
        raise PermissionDenied("Landlord account required.")
    return request.user.landlord_profile


def _group(request, group_id) -> PropertyGroup:
    group = PropertyGroup.objects.filter(pk=group_id, landlord=_landlord(request)).first()
    if not group:
        raise NotFound("Property group not found.")
    return group


def _serialize(area: PropertyArea) -> dict:
    return {
        "id": area.id,
        "area_type": area.area_type,
        "area_type_display": area.get_area_type_display(),
        "count": area.count,
        "description": area.description,
        "shared_with_landlord": area.shared_with_landlord,
        "shared_by_count": area.shared_by.count(),
        "primary_property": area.property_id,
        "primary_property_name": area.property.name,
    }


def _group_common_areas_qs(group: PropertyGroup):
    room_ids = list(group.grouped_properties.values_list("id", flat=True))
    return common_area_queryset(group), room_ids


def _bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def group_common_areas(request, group_id):
    group = _group(request, group_id)
    qs, room_ids = _group_common_areas_qs(group)

    if request.method == "GET":
        return Response([_serialize(a) for a in qs])

    # POST — create a common area shared by every room in the group.
    if len(room_ids) < 2:
        raise ValidationError(
            {"detail": "Add at least two rooms to the group before creating shared common areas."}
        )
    area_type = request.data.get("area_type")
    if area_type not in PropertyArea.AreaType.values:
        raise ValidationError({"area_type": f"Must be one of: {', '.join(PropertyArea.AreaType.values)}"})
    try:
        count = max(int(request.data.get("count", 1)), 1)
    except (TypeError, ValueError):
        raise ValidationError({"count": "Must be a positive integer."})
    area, _created = create_group_common_area(
        group,
        area_type=area_type,
        count=count,
        description=(request.data.get("description") or "").strip(),
        shared_with_landlord=_bool(request.data.get("shared_with_landlord", False)),
    )
    return Response(_serialize(area), status=201)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def group_common_area_detail(request, group_id, area_id):
    group = _group(request, group_id)
    qs, _room_ids = _group_common_areas_qs(group)
    area = qs.filter(id=area_id).first()
    if not area:
        raise NotFound("Common area not found in this group.")

    if request.method == "DELETE":
        area.delete()
        return Response(status=204)

    data = request.data
    shared_with_landlord = None
    if "shared_with_landlord" in data:
        shared_with_landlord = _bool(data["shared_with_landlord"])
    count = None
    if "count" in data:
        try:
            count = max(int(data["count"]), 1)
        except (TypeError, ValueError):
            raise ValidationError({"count": "Must be a positive integer."})
    area = update_group_common_area(
        group,
        area,
        count=count,
        description=(
            (data["description"] or "").strip()
            if "description" in data
            else None
        ),
        shared_with_landlord=shared_with_landlord,
    )
    return Response(_serialize(area))
