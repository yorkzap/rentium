"""
Temporal occupancy log — the "who lived where when" table.

Room/tenant assignments on LeaseTenant are *current state* and get mutated
as roommates come and go. Retroactive billing (utility splits for a past
period) and future AI questions ("who was in room 2 last August?") need
history, so we keep an append-style log:

    Room      Tenant     Move-in      Move-out    Lease
    Room 2    Tenant-A   2025-01-01   2025-08-31  L-1
    Room 2    Tenant-B   2025-09-01   (ongoing)   L-1

Rows are opened on lease activation and closed on move-out/termination.
Corrections follow ledger discipline: close the wrong row, open a right one
— history rows are never edited in place by app code.

IMPORTANT: add `from .occupancy import Occupancy  # noqa` at the bottom of
rentium/leases/models.py so migrations pick this model up.
"""

import uuid
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from rentium.properties.models import Property
from rentium.users.models import TenantProfile


class Occupancy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name="occupancies",
        help_text=_("The room (for group/roommate setups) or the whole unit."),
    )
    tenant = models.ForeignKey(TenantProfile, on_delete=models.PROTECT, related_name="occupancies")
    lease = models.ForeignKey(
        "leases.Lease", on_delete=models.SET_NULL, null=True, blank=True, related_name="occupancies"
    )
    move_in = models.DateField(_("Move-in"))
    move_out = models.DateField(_("Move-out"), null=True, blank=True, help_text=_("Blank = ongoing."))
    note = models.CharField(_("Note"), max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["room", "move_in"]
        verbose_name = _("Occupancy")
        verbose_name_plural = _("Occupancies")
        indexes = [models.Index(fields=["room", "move_in"]), models.Index(fields=["tenant", "move_in"])]

    def __str__(self):
        end = self.move_out or "ongoing"
        return f"{self.tenant} in {self.room} ({self.move_in} → {end})"

    def clean(self):
        super().clean()
        if self.move_out and self.move_out < self.move_in:
            raise ValidationError({"move_out": _("Move-out cannot precede move-in.")})


# ------------------------------------------------------------ lifecycle
def _room_for(lease_tenant) -> Property | None:
    """A roommate lease assigns a room on the LeaseTenant; a whole-unit lease
    occupies lease.property."""
    return getattr(lease_tenant, "room", None) or (
        lease_tenant.lease.property if lease_tenant.lease.property_id else None
    )


def open_occupancy(lease_tenant, move_in: date | None = None) -> Occupancy | None:
    """Idempotent: won't open a second ongoing row for the same room+tenant+lease."""
    room = _room_for(lease_tenant)
    if room is None or lease_tenant.tenant_id is None:
        return None
    move_in = move_in or (
        lease_tenant.individual_start_date
        or lease_tenant.lease.move_in_date
        or lease_tenant.lease.start_date
    )
    existing = Occupancy.objects.filter(
        room=room, tenant=lease_tenant.tenant, lease=lease_tenant.lease, move_out__isnull=True
    ).first()
    if existing:
        return existing
    occ = Occupancy(room=room, tenant=lease_tenant.tenant, lease=lease_tenant.lease, move_in=move_in)
    occ.full_clean()
    occ.save()
    return occ


def close_occupancy(lease_tenant, move_out: date | None = None) -> int:
    """Close all ongoing rows for this tenant on this lease. Returns count."""
    move_out = move_out or date.today()
    qs = Occupancy.objects.filter(
        tenant=lease_tenant.tenant, lease=lease_tenant.lease, move_out__isnull=True
    )
    count = 0
    for occ in qs:
        occ.move_out = max(move_out, occ.move_in)
        occ.full_clean()
        occ.save(update_fields=["move_out"])
        count += 1
    return count


def close_lease_occupancies(lease, move_out: date | None = None) -> int:
    move_out = move_out or date.today()
    count = 0
    for occ in Occupancy.objects.filter(lease=lease, move_out__isnull=True):
        occ.move_out = max(move_out, occ.move_in)
        occ.save(update_fields=["move_out"])
        count += 1
    return count


# ------------------------------------------------------------ queries
def occupant_days_for_lease(lease, period_start: date, period_end: date) -> dict:
    """
    {tenant: days_present} during [period_start, period_end] (inclusive),
    from the occupancy log. Empty dict if no rows exist for the lease —
    callers fall back to an equal split.
    """
    rows = Occupancy.objects.filter(lease=lease, move_in__lte=period_end).filter(
        models.Q(move_out__isnull=True) | models.Q(move_out__gte=period_start)
    ).select_related("tenant")

    weights: dict = {}
    for occ in rows:
        start = max(occ.move_in, period_start)
        end = min(occ.move_out or period_end, period_end)
        days = (end - start).days + 1
        if days > 0:
            weights[occ.tenant] = weights.get(occ.tenant, 0) + days
    return weights
