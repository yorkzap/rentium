"""
Derived-state signals for the properties app.

Two things are kept honest here, both for the same reason: they're DERIVED, and
a derived value that only updates on one code path is a derived value that will
eventually be wrong. Django admin, a shell, a fixture import and a management
command all write models without going anywhere near a viewset — so the
recompute has to live at the model layer, which means signals.

  is_furnished   from the property's inventory (properties/furnishing.py)
  coordinates    from the property's address (rentium/core/geo.py, async)

Both use .update() rather than .save(), so we never recurse into Property.save()
/ full_clean(), and never clobber a concurrent edit to an unrelated field.

WIRING: properties/apps.py ready() imports this module.
"""

import logging

from django.db.models.signals import post_delete
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


# --- is_furnished --------------------------------------------------------
def _recompute_furnishing(prop) -> None:
    from .furnishing import compute_is_furnished
    from .models import Property

    try:
        value = compute_is_furnished(prop)
        if prop.is_furnished != value:
            Property.objects.filter(pk=prop.pk).update(is_furnished=value)
    except Exception:  # a derived flag must never break an inventory save
        logger.exception("Furnishing recompute failed for property %s", prop.pk)


@receiver(
    post_save, sender="properties.InventoryItem", dispatch_uid="furnish_on_item_save"
)
def furnish_on_item_save(sender, instance, raw=False, **kwargs):
    if raw or not instance.property_id:
        return
    _recompute_furnishing(instance.property)


@receiver(
    post_delete,
    sender="properties.InventoryItem",
    dispatch_uid="furnish_on_item_delete",
)
def furnish_on_item_delete(sender, instance, **kwargs):
    from .models import Property

    prop = Property.objects.filter(pk=instance.property_id).first()
    if prop:
        _recompute_furnishing(prop)


# --- coordinates ---------------------------------------------------------
@receiver(
    post_save, sender="properties.Property", dispatch_uid="geocode_on_address_change"
)
def geocode_on_address_change(sender, instance, created, raw=False, **kwargs):
    """
    Enqueue a geocode when a property has an address but no coordinates.

    In the normal path this never fires: the address autocomplete already handed
    us lat/lng when the landlord picked from the dropdown. It's here for the
    paths that bypass the form — admin, imports, a landlord who typed the address
    by hand — so a property never sits on the public city page as a listing with
    no map pin, silently looking broken.

    on_commit, because the Celery worker will read this row and it has to exist
    in the database first. A .delay() inside the transaction races the commit and
    intermittently geocodes nothing at all — which is exactly the kind of bug
    that only shows up in production under load.
    """
    if raw or not instance.address:
        return
    if instance.latitude is not None:
        return

    from django.db import transaction

    from rentium.showcase.tasks import geocode_property

    def _enqueue():
        try:
            geocode_property.delay(instance.pk)
        except Exception:  # broker down — the hourly sweep will catch it
            logger.exception("Could not enqueue geocode for property %s", instance.pk)

    transaction.on_commit(_enqueue)
