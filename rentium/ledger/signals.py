# rentium/ledger/signals.py
"""
Integration signals — the glue that fires business hooks no matter how a row
is created (API, admin, shell, or import). Wired in LedgerConfig.ready().

Everything here is idempotent and skips fixture loads (`raw`), so it's safe
to fire on every save. Lazy string senders ("app.Model") avoid import cycles.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger(__name__)


# --- RentAdjustment -> ledger reconciliation ------------------------------
@receiver(
    post_save, sender="leases.RentAdjustment", dispatch_uid="adjustment_to_ledger"
)
def apply_rent_adjustment(sender, instance, created, raw=False, **kwargs):
    """
    When a rent adjustment lands on an ACTIVE lease, reconcile existing rent
    charges (void+repost unpaid ones, credit partially-paid ones — never
    mutate a posted entry). Before activation there are no charges yet, so
    this is a no-op and the first charge is simply generated at the adjusted
    amount. Re-saves are naturally idempotent.
    """
    if raw:
        return
    lease = instance.lease_tenant.lease
    if lease.status != lease.LeaseStatus.ACTIVE:
        return
    try:
        from rentium.ledger.billing import apply_adjustment_to_ledger

        apply_adjustment_to_ledger(instance.lease_tenant, instance)
    except Exception:  # never let a hook break the save
        logger.exception(
            "apply_adjustment_to_ledger failed for adjustment %s", instance.pk
        )


# --- Default areas on property / group creation ---------------------------
@receiver(post_save, sender="properties.PropertyGroup", dispatch_uid="seed_group_areas")
def seed_group_areas(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    try:
        from rentium.properties.areas import seed_default_areas

        seed_default_areas(group=instance)
    except Exception:
        logger.exception("seed_default_areas(group=%s) failed", instance.pk)


@receiver(post_save, sender="properties.Property", dispatch_uid="seed_property_areas")
def seed_property_areas(sender, instance, created, raw=False, **kwargs):
    """Standalone complete units get their own areas; rooms rely on the
    group's areas (plus the room itself).

    A listing attached to a PropertyUnit seeds the UNIT instead: the physical
    space owns the layout, and the listing is only an offer on it. Seeding is
    idempotent, so several listings on one unit converge rather than duplicate.
    """
    if raw or not created:
        return
    from rentium.properties.models import Property

    if instance.property_category != Property.PropertyCategory.COMPLETE_UNIT:
        return
    try:
        from rentium.properties.areas import seed_default_areas

        if instance.unit_id:
            seed_default_areas(unit=instance.unit)
        else:
            seed_default_areas(property=instance)
    except Exception:
        logger.exception("seed_default_areas(property=%s) failed", instance.pk)


# --- Lease end -> close occupancy log -------------------------------------
@receiver(post_save, sender="leases.Lease", dispatch_uid="close_occupancies_on_end")
def close_occupancies_on_end(sender, instance, raw=False, **kwargs):
    """Terminated/expired leases close their open occupancy rows. Idempotent:
    only rows still marked ongoing are touched."""
    if raw:
        return
    if instance.status not in (
        instance.LeaseStatus.TERMINATED,
        instance.LeaseStatus.EXPIRED,
    ):
        return
    try:
        from rentium.leases.occupancy import close_lease_occupancies

        move_out = instance.move_out_date or instance.end_date or timezone.now().date()
        close_lease_occupancies(instance, move_out=move_out)
    except Exception:
        logger.exception("close_lease_occupancies failed for lease %s", instance.pk)


# --- LeaseTenant join/leave on an ACTIVE lease ----------------------------
@receiver(post_save, sender="leases.LeaseTenant", dispatch_uid="sync_tenant_occupancy")
def sync_tenant_occupancy(sender, instance, raw=False, **kwargs):
    """
    Handles roommates who join or leave a lease that is ALREADY active
    (initial activation is handled explicitly in check_and_activate):

    - a tenant who becomes LINKED (claims their invite / gets attached to a
      TenantProfile)          -> open their occupancy + materialize their
      rent schedule right away (don't wait for the nightly task);
    - a tenant who declines   -> close their occupancy.

    NOTE — linked, not signed, is deliberately the trigger. This mirrors
    check_and_activate(), which opens occupancy and generates charges for
    every lease_tenants.filter(tenant__isnull=False, declined=False) row,
    signed or not — and generate_rent_charges_for_lease() itself uses the
    same filter. Per the joint-and-several policy, an unsigned-but-linked
    tenant still owes rent, so "Expected This Month" must include them and
    utility splits must weight them. The previous version gated all of
    this on instance.has_signed, so a tenant who linked their account
    AFTER activation never got a rent charge or occupancy row — that made
    the landlord dashboard show only the signed tenants' shares (e.g.
    $600 instead of $1,200 on a two-tenant, $1,200 lease) and dumped 100%
    of any utility split on the signed tenant.

    All operations are idempotent (occupancy open/close checks state;
    charges use natural idempotency keys), so re-saves are safe.
    """
    if raw:
        return
    lease = instance.lease
    if lease.status != lease.LeaseStatus.ACTIVE:
        return
    try:
        from rentium.leases.occupancy import close_occupancy
        from rentium.leases.occupancy import open_occupancy

        if instance.declined:
            close_occupancy(instance)
            return
        if instance.tenant_id:
            open_occupancy(instance)
            from rentium.ledger.billing import generate_rent_charges_for_lease

            generate_rent_charges_for_lease(lease)
    except Exception:
        logger.exception(
            "sync_tenant_occupancy failed for lease_tenant %s", instance.pk
        )
