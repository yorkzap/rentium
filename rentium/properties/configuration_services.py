"""Previewed, dated rental arrangements with unit-level concurrency control."""

import hashlib
import json
from datetime import date

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from rentium.leases.space_services import (
    LIVE_STATUSES,
    leases_for_unit,
    occupancy_state,
    scheduled_end,
    space_scope,
)

from .models import Property, PropertyArea, PropertyGroup, PropertyUnit
from .rental_configuration import RentalConfiguration

SALT = "rentium.rental-configuration.v1"


def configuration_data(configuration):
    return {
        "id": str(configuration.pk),
        "unit_id": str(configuration.unit_id),
        "mode": configuration.mode,
        "effective_from": configuration.effective_from.isoformat(),
        "status": configuration.status,
        "offering_ids": configuration.offering_ids,
        "blocked_reason": configuration.blocked_reason,
        "approved_at": configuration.approved_at.isoformat(),
    }


def _fingerprint(unit):
    rows = {
        "unit": [str(unit.pk), unit.rental_mode, unit.updated_at.isoformat()],
        "leases": [
            {
                "id": str(lease.pk),
                "status": lease.status,
                "start": str(lease.start_date),
                "end": str(scheduled_end(lease)),
                "updated": str(lease.updated_at),
                "scope": space_scope(lease),
                "occupancy": occupancy_state(lease),
            }
            for lease in leases_for_unit(unit).order_by("pk")
        ],
        "offerings": list(
            unit.offerings.order_by("pk").values(
                "pk", "name", "property_category", "is_active_offering", "updated_at"
            )
        ),
        "configurations": list(
            unit.rental_configurations.order_by("pk").values(
                "pk", "status", "effective_from"
            )
        ),
        "areas": list(
            unit.areas.order_by("pk").values(
                "pk", "area_type", "exclusive_to_id", "is_seeded_default"
            )
        ),
    }
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode()
    ).hexdigest()


def preview_configuration(
    unit, *, mode, effective_from, offering_ids=None, exclude_configuration=None
):
    try:
        day = date.fromisoformat(str(effective_from))
    except (ValueError, TypeError) as exc:
        raise ValidationError("Enter an effective date in YYYY-MM-DD format.") from exc
    if mode not in {"WHOLE_UNIT", "BY_ROOM"}:
        raise ValidationError("Choose whole unit or rooms.")
    if day < timezone.localdate() and exclude_configuration is None:
        raise ValidationError("Rental configuration changes cannot be backdated.")
    category = "COMPLETE_UNIT" if mode == "WHOLE_UNIT" else "ROOM"
    offerings = unit.offerings.filter(property_category=category)
    bedrooms = list(
        unit.areas.filter(area_type="BEDROOM", is_seeded_default=False).order_by("pk")
    )
    if mode == "BY_ROOM" and offering_ids is None:
        offerings = offerings.filter(pk__in=[area.exclusive_to_id for area in bedrooms])
    if offering_ids is not None:
        offerings = offerings.filter(pk__in=offering_ids)
    offerings = list(offerings.order_by("pk"))
    if mode == "WHOLE_UNIT" and len(offerings) > 1:
        raise ValidationError(
            "Choose the whole-unit listing to offer; this unit has several."
        )
    will_create = []
    if offering_ids is None:
        if mode == "WHOLE_UNIT" and not offerings:
            will_create = [{"name": unit.name, "category": category, "area_id": None}]
        elif mode == "BY_ROOM":
            will_create = [
                {"name": area.name, "category": category, "area_id": str(area.pk)}
                for area in bedrooms
                if not area.exclusive_to_id
            ]
    if not offerings and not will_create:
        raise ValidationError(
            "Record the physical bedrooms in Layout before offering rooms."
        )
    template = unit.offerings.order_by("pk").first()
    if will_create and not template:
        raise ValidationError(
            "Create a first listing with verified location details before preparing another arrangement."
        )
    if offering_ids is not None and {str(p.pk) for p in offerings} != {
        str(pk) for pk in offering_ids
    }:
        raise ValidationError(
            "All selected listings must belong to this unit and rental mode."
        )
    if mode == "BY_ROOM":
        mapped = set(
            PropertyArea.objects.filter(
                unit=unit, area_type="BEDROOM", is_seeded_default=False
            ).values_list("exclusive_to_id", flat=True)
        )
        if any(p.pk not in mapped for p in offerings):
            raise ValidationError(
                "Map each room listing to its physical bedroom before offering the rooms."
            )
    blockers = []
    for lease in leases_for_unit(unit).filter(status__in=LIVE_STATUSES):
        end = scheduled_end(lease)
        scope = space_scope(lease)
        compatible = scope["coverage"] == "COMPLETE" and scope["whole_unit"] == (
            mode == "WHOLE_UNIT"
        )
        if mode == "BY_ROOM":
            offered_rooms = {
                str(area.pk)
                for area in bedrooms
                if area.exclusive_to_id in {p.pk for p in offerings}
            }
            compatible = compatible and set(scope["room_ids"]) <= offered_rooms
        if compatible:
            # Aligning marketing with the space already granted does not end
            # that tenancy or require the tenant to leave.
            continue
        if lease.start_date >= day:
            blockers.append(
                {
                    "lease": lease.lease_number,
                    "reason": "A future commitment has a different or unresolved scope.",
                }
            )
        elif not end or end >= day:
            blockers.append(
                {
                    "lease": lease.lease_number,
                    "reason": "No established tenancy end before this configuration starts.",
                }
            )
        elif (
            day <= timezone.localdate()
            and occupancy_state(lease) != "RECORDED_DEPARTED"
        ):
            blockers.append(
                {
                    "lease": lease.lease_number,
                    "reason": "Departure has not been confirmed.",
                }
            )
    configurations = unit.rental_configurations.filter(
        status__in=["ACTIVE", "SCHEDULED", "BLOCKED"]
    ).exclude(pk=exclude_configuration)
    if configurations.filter(effective_from=day).exists():
        blockers.append(
            {"reason": "An approved configuration already starts on this date."}
        )
    # One unactivated transition at a time avoids implicitly replacing a future
    # approved offering set. Cancel it explicitly before preparing another.
    if configurations.filter(status__in=["SCHEDULED", "BLOCKED"]).exists():
        blockers.append(
            {"reason": "Review or cancel the existing future arrangement first."}
        )
    payload = {
        "unit_id": str(unit.pk),
        "landlord_id": str(unit.landlord_id),
        "mode": mode,
        "effective_from": day.isoformat(),
        "offering_ids": [p.pk for p in offerings],
        "will_create": will_create,
        "selection": offering_ids,
        "fingerprint": _fingerprint(unit),
    }
    return {
        **payload,
        "ok": not blockers,
        "blocked_by": blockers,
        "unit": unit.name,
        "will_offer": [{"id": p.pk, "name": p.name} for p in offerings],
        "will_park": list(
            unit.offerings.filter(is_active_offering=True)
            .exclude(pk__in=payload["offering_ids"])
            .values("id", "name")
        ),
        "note": "Existing agreements and physical spaces stay unchanged. Future availability is conditional on departure and final conflict checks.",
        "preview_token": signing.dumps(payload, salt=SALT) if not blockers else None,
    }


@transaction.atomic
def confirm_configuration(unit, *, preview_token, user):
    unit = PropertyUnit.objects.select_for_update().get(pk=unit.pk)
    if user.pk != unit.landlord.user_id:
        raise ValidationError(
            "Only the portfolio owner can approve a rental arrangement."
        )
    try:
        payload = signing.loads(preview_token, salt=SALT, max_age=1800)
    except signing.BadSignature as exc:
        raise ValidationError(
            "This preview has expired or is invalid; review a new preview."
        ) from exc
    if payload["unit_id"] != str(unit.pk) or payload["landlord_id"] != str(
        unit.landlord_id
    ):
        raise ValidationError("This preview belongs to another unit.")
    key = hashlib.sha256(preview_token.encode()).hexdigest()
    existing = RentalConfiguration.objects.filter(receipt_key=key).first()
    if existing:
        return configuration_data(existing)
    if payload["fingerprint"] != _fingerprint(unit):
        raise ValidationError(
            "The unit or its commitments changed. Review a new preview."
        )
    preview = preview_configuration(
        unit,
        mode=payload["mode"],
        effective_from=payload["effective_from"],
        offering_ids=payload["selection"],
    )
    if not preview["ok"]:
        raise ValidationError(
            "The configuration is blocked; review its current commitments."
        )
    ids = list(payload["offering_ids"])
    template = unit.offerings.order_by("pk").first()
    for proposed in payload["will_create"]:
        group = None
        if proposed["category"] == "ROOM":
            group, _ = PropertyGroup.objects.get_or_create(
                unit=unit,
                defaults={"landlord": unit.landlord, "name": f"{unit.name} rooms"},
            )
        listing = Property.objects.create(
            landlord=unit.landlord,
            holding=unit.holding,
            unit=unit,
            group=group,
            name=proposed["name"],
            property_category=proposed["category"],
            room_type="PRIVATE" if group else None,
            address=template.address,
            city=template.city,
            province=template.province,
            postal_code=template.postal_code,
            country=template.country,
            is_active_offering=False,
            is_publicly_visible=False,
            available_from=payload["effective_from"],
        )
        ids.append(listing.pk)
        if proposed["area_id"]:
            PropertyArea.objects.filter(pk=proposed["area_id"], unit=unit).update(
                exclusive_to=listing
            )
    configuration = RentalConfiguration.objects.create(
        unit=unit,
        mode=payload["mode"],
        effective_from=date.fromisoformat(payload["effective_from"]),
        offering_ids=ids,
        snapshot=preview,
        receipt_key=key,
        approved_by=user,
    )
    configuration.offerings.set(ids)
    unit.offerings.filter(pk__in=ids, is_active_offering=False).update(
        available_from=payload["effective_from"]
    )
    if date.fromisoformat(payload["effective_from"]) <= timezone.localdate():
        activate_configuration(configuration)
    return configuration_data(configuration)


@transaction.atomic
def activate_configuration(configuration):
    unit = PropertyUnit.objects.select_for_update().get(pk=configuration.unit_id)
    configuration.refresh_from_db()
    if (
        configuration.status not in {"SCHEDULED", "BLOCKED"}
        or configuration.effective_from > timezone.localdate()
    ):
        return configuration
    try:
        review = preview_configuration(
            unit,
            mode=configuration.mode,
            effective_from=configuration.effective_from,
            offering_ids=configuration.offering_ids,
            exclude_configuration=configuration.pk,
        )
        if not review["ok"]:
            raise ValidationError(
                "; ".join(row["reason"] for row in review["blocked_by"])
            )
    except ValidationError as exc:
        configuration.status = "BLOCKED"
        configuration.blocked_reason = "; ".join(exc.messages)
        configuration.save(update_fields=["status", "blocked_reason"])
        return configuration
    unit.rental_configurations.filter(status="ACTIVE").exclude(
        pk=configuration.pk
    ).update(status="SUPERSEDED")
    unit.offerings.update(is_active_offering=False)
    unit.offerings.filter(pk__in=configuration.offering_ids).update(
        is_active_offering=True
    )
    unit.rental_mode = configuration.mode
    unit.save(update_fields=["rental_mode", "updated_at"])
    configuration.status = "ACTIVE"
    configuration.blocked_reason = ""
    configuration.save(update_fields=["status", "blocked_reason"])
    return configuration


@transaction.atomic
def cancel_configuration(unit, configuration_id):
    unit = PropertyUnit.objects.select_for_update().get(pk=unit.pk)
    configuration = unit.rental_configurations.get(pk=configuration_id)
    if configuration.status == "CANCELLED":
        return configuration_data(configuration)
    if configuration.status not in {"SCHEDULED", "BLOCKED"}:
        raise ValidationError("Only future or blocked configurations can be cancelled.")
    if (
        leases_for_unit(unit)
        .filter(status__in=LIVE_STATUSES, start_date__gte=configuration.effective_from)
        .exists()
    ):
        raise ValidationError(
            "Resolve the dependent future leases before cancelling this configuration."
        )
    configuration.status = "CANCELLED"
    configuration.save(update_fields=["status"])
    return configuration_data(configuration)


def unit_overview(unit):
    from rentium.leases.tenancy_summary import tenancy_summary

    return {
        "unit_id": str(unit.pk),
        "name": unit.name,
        "configurations": [
            configuration_data(row) for row in unit.rental_configurations.all()
        ],
        "tenancies": [
            tenancy_summary(lease)
            for lease in leases_for_unit(unit).order_by("-start_date")
        ],
    }
