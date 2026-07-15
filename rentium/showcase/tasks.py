"""
Geo housekeeping.

Autocomplete gives us coordinates the moment a landlord picks an address, so in
the normal path there is nothing to do here. These tasks exist for the two
abnormal paths: properties that predate autocomplete, and addresses that were
typed by hand or imported.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def geocode_property(property_id: int):
    """
    Resolve one property's address to coordinates + neighbourhood.

    Enqueued on save when the address changed (properties/signals.py) so a new
    property has a map pin within seconds rather than waiting up to an hour for
    the sweep below — a property that appears on the city page with no pin looks
    broken, and it looks broken to the person we most want to impress.
    """
    from rentium.core.geo import geocode
    from rentium.properties.models import Property
    from rentium.properties.models import normalise_postal_code

    prop = Property.objects.filter(pk=property_id).first()
    if not prop or not prop.address:
        return False

    result = geocode(
        prop.address, prop.city, prop.get_province_display() if prop.province else ""
    )
    if not result:
        logger.info("No geocode match for property %s (%s)", prop.pk, prop.address)
        return False

    from django.utils import timezone

    updates = {
        "latitude": result["latitude"],
        "longitude": result["longitude"],
        "geocoded_at": timezone.now(),
    }
    # Never overwrite what the landlord typed themselves — the neighbourhood is
    # the ONLY location a stranger ever sees, so how their place is described is
    # their editorial call, not the geocoder's.
    if result["neighbourhood"] and not prop.neighbourhood:
        updates["neighbourhood"] = result["neighbourhood"]
    if result["province_code"] and not prop.province:
        updates["province"] = result["province_code"]
        updates["province_code"] = result["province_code"]
    if result["postal_code"] and not prop.postal_code:
        updates["postal_code"] = normalise_postal_code(result["postal_code"])

    Property.objects.filter(pk=prop.pk).update(**updates)
    return True


@shared_task
def geocode_pending(limit: int = 50):
    """Hourly sweep. The net that catches anything the save-time task missed."""
    from rentium.properties.models import Property

    pending = Property.objects.filter(latitude__isnull=True).exclude(address="")[:limit]
    done = 0
    for prop in pending:
        if geocode_property(prop.pk):
            done += 1
    return {"geocoded": done}
