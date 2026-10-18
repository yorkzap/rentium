"""Space and time invariants shared by leases, configurations, REST and RAMA."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

LIVE_STATUSES = ("DRAFT", "PENDING", "ACTIVE")


def lease_unit(lease):
    if lease.property_id:
        return lease.property.unit
    if lease.group_id:
        return getattr(lease.group, "unit", None)
    return None


def leases_for_unit(unit):
    from .models import Lease

    return (
        Lease.objects.filter(
            Q(property__unit=unit) | Q(group__unit=unit) | Q(scope_versions__unit=unit),
        )
        .distinct()
        .select_related("property", "group")
    )


def space_scope(lease):
    """Legacy inference is explicit and conservative; never infer from names."""
    revision = lease.scope_versions.first() if not lease._state.adding else None
    if revision:
        return {
            "unit_id": str(revision.unit_id),
            "whole_unit": revision.whole_unit,
            "room_ids": revision.room_ids,
            "coverage": "COMPLETE",
            "source_id": str(revision.pk),
            "source": "leases.LeaseSpaceScope",
        }
    unit = lease_unit(lease)
    if unit is None:
        return {
            "unit_id": None,
            "whole_unit": False,
            "room_ids": [],
            "coverage": "PARTIAL",
        }
    if lease.property_id and lease.property.property_category == "COMPLETE_UNIT":
        return {
            "unit_id": str(unit.pk),
            "whole_unit": True,
            "room_ids": [],
            "coverage": "COMPLETE",
        }
    from rentium.properties.models import PropertyArea

    listing_ids = (
        [lease.property_id]
        if lease.property_id
        else list(
            lease.lease_tenants.filter(room__isnull=False).values_list(
                "room_id", flat=True
            )
        )
        if not lease._state.adding
        else []
    )
    rooms = list(
        PropertyArea.objects.filter(
            unit=unit,
            area_type="BEDROOM",
            is_seeded_default=False,
            exclusive_to_id__in=listing_ids,
        ).values_list("pk", flat=True)
    )
    return {
        "unit_id": str(unit.pk),
        "whole_unit": False,
        "room_ids": [str(pk) for pk in rooms],
        "coverage": "COMPLETE"
        if listing_ids and len(rooms) == len(set(listing_ids))
        else "PARTIAL",
    }


def scheduled_end(lease):
    """A term end is not necessarily a tenancy end (e.g. BC continuation)."""
    notices = (
        list(
            lease.moveout_requests.filter(
                status="ACCEPTED", effective_end_date__isnull=False
            )
        )
        if not lease._state.adding
        else []
    )
    ends = {notice.effective_end_date for notice in notices}
    if len(ends) > 1:
        return None
    if ends:
        return next(iter(ends))
    if lease.move_out_date:
        return lease.move_out_date
    if lease.status not in LIVE_STATUSES:
        return lease.end_date
    # Do not promise a new tenancy based solely on a fixed-term date.
    return None


def intervals_overlap(first, second):
    first_end, second_end = scheduled_end(first), scheduled_end(second)
    return not (
        (first_end and first_end < second.start_date)
        or (second_end and second_end < first.start_date)
    )


def scopes_overlap(first, second):
    if first["unit_id"] != second["unit_id"]:
        return False
    if first["whole_unit"] or second["whole_unit"]:
        return True
    if "PARTIAL" in (first["coverage"], second["coverage"]):
        return True  # Unknown coverage cannot establish that space is free.
    return bool(set(first["room_ids"]) & set(second["room_ids"]))


def validate_space_commitment(lease, *, proposed_scope=None):
    if lease.status not in LIVE_STATUSES:
        return
    scope = proposed_scope or space_scope(lease)
    if not scope["unit_id"]:
        return
    from rentium.properties.models import PropertyUnit

    unit = PropertyUnit.objects.get(pk=scope["unit_id"])
    configurations = list(
        unit.rental_configurations.filter(
            status__in=["ACTIVE", "SCHEDULED", "BLOCKED"]
        ).order_by("effective_from")
    )
    for index, configuration in enumerate(configurations):
        next_start = (
            configurations[index + 1].effective_from
            if index + 1 < len(configurations)
            else None
        )
        end = scheduled_end(lease)
        if next_start and lease.start_date >= next_start:
            continue
        if end and end < configuration.effective_from:
            continue
        compatible = scope["coverage"] == "COMPLETE" and scope["whole_unit"] == (
            configuration.mode == "WHOLE_UNIT"
        )
        if compatible and not scope["whole_unit"]:
            from rentium.properties.models import PropertyArea

            room_ids = {
                str(pk)
                for pk in PropertyArea.objects.filter(
                    unit=unit,
                    exclusive_to_id__in=configuration.offering_ids,
                    area_type="BEDROOM",
                    is_seeded_default=False,
                ).values_list("pk", flat=True)
            }
            compatible = set(scope["room_ids"]) <= room_ids
        if not compatible:
            raise ValidationError(
                f"This tenancy conflicts with the approved rental arrangement starting {configuration.effective_from}. Review that arrangement or the tenancy scope first."
            )
    for other in (
        leases_for_unit(unit).filter(status__in=LIVE_STATUSES).exclude(pk=lease.pk)
    ):
        other_scope = space_scope(other)
        if intervals_overlap(lease, other) and scopes_overlap(scope, other_scope):
            # Preserve existing unmapped room records until the reviewed scope
            # migration. Once either side is mapped, uncertainty blocks writes.
            if (
                not lease._state.adding
                and not proposed_scope
                and not scope.get("source_id")
                and not other_scope.get("source_id")
            ):
                # Existing unresolved records remain readable/editable while
                # reviewed corrections are prepared. Never grandfather a new
                # overlapping commitment.
                continue
            raise ValidationError(
                f"Space overlaps {'whole-unit lease' if other_scope['whole_unit'] else 'room lease'} {other.lease_number}. Resolve its end date or space scope before committing this tenancy."
            )


@transaction.atomic
def record_space_scope(lease, *, unit, whole_unit, room_ids, user, reason, receipt=""):
    from rentium.properties.models import PropertyUnit
    from .tenancy_state import LeaseSpaceScope

    unit = PropertyUnit.objects.select_for_update().get(pk=unit.pk)
    if lease.landlord_id != unit.landlord_id:
        raise ValidationError("The unit is outside this portfolio.")
    old_unit = lease_unit(lease)
    if old_unit and old_unit.pk != unit.pk:
        raise ValidationError(
            "Move the listing through a reviewed hierarchy correction first."
        )
    scope = LeaseSpaceScope(
        lease=lease,
        unit=unit,
        whole_unit=whole_unit,
        room_ids=sorted({str(pk) for pk in room_ids}),
        recorded_by=user,
        reason=reason,
        snapshot={
            "unit": unit.name,
            "address": unit.holding.address,
            "review_receipt": receipt,
        },
    )
    scope.full_clean()
    proposed = {
        "unit_id": str(unit.pk),
        "whole_unit": whole_unit,
        "room_ids": scope.room_ids,
        "coverage": "COMPLETE",
    }
    validate_space_commitment(lease, proposed_scope=proposed)
    latest = lease.scope_versions.first()
    if (
        latest
        and latest.unit_id == unit.pk
        and latest.whole_unit == whole_unit
        and latest.room_ids == scope.room_ids
    ):
        return latest
    scope.save()
    return scope


def occupancy_state(lease, *, as_of=None):
    """Recorded presence is independent of signing and expected possession."""
    from django.utils import timezone

    day = as_of or timezone.localdate()
    event = lease.presence_events.filter(occurred_on__lte=day).first()
    if event:
        return "RECORDED_OCCUPIED" if event.state == "PRESENT" else "RECORDED_DEPARTED"
    rows = list(lease.occupancies.all())
    present = [
        row
        for row in rows
        if row.move_in <= day and (row.move_out is None or row.move_out >= day)
    ]
    if present:
        return "RECORDED_OCCUPIED"
    if rows and all(row.move_out and row.move_out < day for row in rows):
        return "RECORDED_DEPARTED"
    if (lease.move_in_date or lease.start_date) > day:
        return "EXPECTED_FUTURE"
    return "UNCONFIRMED"
