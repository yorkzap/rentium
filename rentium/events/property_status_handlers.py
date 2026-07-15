"""
Property status automation — event handlers.

WIRING: import this module wherever your existing handlers are loaded so
the @on registrations run (typically rentium/events/apps.py):

    class EventsConfig(AppConfig):
        def ready(self):
            from . import handlers  # existing
            from . import property_status_handlers  # noqa: F401  <- add

Rules (deliberately conservative — MANUAL OVERRIDE ALWAYS WINS):
Every transition here only fires FROM the specific status automation
expects to find. If the landlord has set anything else by hand
(NOT_AVAILABLE, or forced OCCUPIED/AVAILABLE), we leave it alone —
automation never fights a human.

  lease.activated             AVAILABLE -> OCCUPIED   (the leased property,
                                                       or each room a
                                                       group-lease tenant
                                                       is assigned to)
  lease.terminated /
  lease.moveout_accepted*     OCCUPIED  -> AVAILABLE  (only if no OTHER
                                                       active lease still
                                                       covers it)
  maintenance.status_changed:
    -> IN_PROGRESS            AVAILABLE -> MAINTENANCE
    -> COMPLETED/CANCELLED    MAINTENANCE -> OCCUPIED if an active lease
                                             covers it, else AVAILABLE

* moveout_accepted fires when the agreement is signed, which may be before
  the effective end date — we only flip the status if the effective end
  has actually arrived; otherwise the eventual lease.terminated/expiry
  handles it.
"""
import logging
from datetime import date

from .registry import on

logger = logging.getLogger(__name__)


def _properties_for_lease(lease):
    """The property (single-property lease) or the specific rooms tenants
    occupy (group lease)."""
    from rentium.properties.models import Property

    if lease.property_id:
        return list(Property.objects.filter(pk=lease.property_id))
    rooms = [lt.room for lt in lease.lease_tenants.select_related("room") if lt.room_id]
    if rooms:
        return rooms
    if lease.group_id:
        return list(lease.group.grouped_properties.all())
    return []


def _has_other_active_lease(prop, exclude_lease_id=None):
    from rentium.leases.models import Lease, LeaseTenant

    qs = Lease.objects.filter(status=Lease.LeaseStatus.ACTIVE)
    if exclude_lease_id:
        qs = qs.exclude(pk=exclude_lease_id)
    if qs.filter(property=prop).exists():
        return True
    # group leases occupying this room via a tenant assignment
    return LeaseTenant.objects.filter(
        room=prop, lease__status=Lease.LeaseStatus.ACTIVE
    ).exclude(lease_id=exclude_lease_id).exists()


def _transition(prop, from_status, to_status, why):
    from rentium.properties.models import Property

    if prop.status != from_status:
        logger.info(
            "Property %s status is %s (expected %s) — leaving alone (%s)",
            prop.pk, prop.status, from_status, why,
        )
        return
    Property.objects.filter(pk=prop.pk, status=from_status).update(status=to_status)
    logger.info("Property %s: %s -> %s (%s)", prop.pk, from_status, to_status, why)


@on("lease.activated")
def mark_occupied_on_activation(event):
    from rentium.leases.models import Lease
    from rentium.properties.models import Property

    lease = Lease.objects.filter(pk=event.lease_id).first()
    if not lease:
        return
    for prop in _properties_for_lease(lease):
        _transition(
            prop, Property.PropertyStatus.AVAILABLE, Property.PropertyStatus.OCCUPIED,
            f"lease {lease.lease_number} activated",
        )


@on("lease.terminated")
def mark_available_on_termination(event):
    _free_up_after_lease_end(event)


@on("lease.moveout_accepted")
def mark_available_on_moveout(event):
    eff = (event.payload or {}).get("effective_end_date")
    if eff and date.fromisoformat(eff) > date.today():
        return  # signed for a future date; expiry/termination will handle it
    _free_up_after_lease_end(event)


def _free_up_after_lease_end(event):
    from rentium.leases.models import Lease
    from rentium.properties.models import Property

    lease = Lease.objects.filter(pk=event.lease_id).first()
    if not lease:
        return
    for prop in _properties_for_lease(lease):
        if _has_other_active_lease(prop, exclude_lease_id=lease.pk):
            continue
        _transition(
            prop, Property.PropertyStatus.OCCUPIED, Property.PropertyStatus.AVAILABLE,
            f"lease {lease.lease_number} ended",
        )


@on("maintenance.status_changed")
def sync_maintenance_status(event):
    from rentium.properties.models import Property

    payload = event.payload or {}
    new_status = payload.get("status") or payload.get("new_status") or ""
    prop = Property.objects.filter(pk=event.property_id).first() if event.property_id else None
    if not prop:
        wo_id = payload.get("work_order_id")
        if wo_id:
            try:
                from rentium.maintenance.models import WorkOrder

                wo = WorkOrder.objects.filter(pk=wo_id).select_related("property").first()
                prop = wo.property if wo else None
            except Exception:  # app layout differs — property_id should normally be on the event
                prop = None
    if not prop:
        return

    if new_status == "IN_PROGRESS":
        _transition(
            prop, Property.PropertyStatus.AVAILABLE, Property.PropertyStatus.MAINTENANCE,
            "work order in progress",
        )
    elif new_status in ("COMPLETED", "CANCELLED"):
        if prop.status != Property.PropertyStatus.MAINTENANCE:
            return
        target = (
            Property.PropertyStatus.OCCUPIED
            if _has_other_active_lease(prop)
            else Property.PropertyStatus.AVAILABLE
        )
        _transition(prop, Property.PropertyStatus.MAINTENANCE, target, "work order closed")
