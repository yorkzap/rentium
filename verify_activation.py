"""Verify activation wiring end to end WITHOUT persisting anything."""

from django.db import transaction

from rentium.leases.models import Lease
from rentium.leases.occupancy import Occupancy, open_occupancy
from rentium.ledger.billing import generate_initial_charges
from rentium.ledger.models import CHARGE_TYPES, EntryType, LedgerEntry

RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


lease = (
    Lease.objects.filter(lease_tenants__tenant__isnull=False, lease_tenants__has_signed=True)
    .distinct()
    .first()
)
if not lease:
    print("No lease with a signed tenant found. Sign a tenant on a lease, then re-run.")
    raise SystemExit

print(f"\n=== Activation wiring test on lease {lease.pk} (current status={lease.status}) ===\n")

with transaction.atomic():
    sid = transaction.savepoint()

    lease.status = Lease.LeaseStatus.ACTIVE
    lease.save(update_fields=["status", "updated_at"])

    generate_initial_charges(lease)
    signed = list(lease.lease_tenants.filter(tenant__isnull=False, declined=False))
    for lt in signed:
        open_occupancy(lt)

    charges = LedgerEntry.objects.filter(lease=lease, entry_type__in=CHARGE_TYPES)
    rent = charges.filter(entry_type=EntryType.RENT_CHARGE)
    deposits = charges.filter(entry_type=EntryType.DEPOSIT_CHARGE)
    occ = Occupancy.objects.filter(lease=lease)

    check("rent charges generated", rent.exists())
    if lease.security_deposit and lease.security_deposit > 0:
        check("security deposit charge created", deposits.exists())
    else:
        print("  [skip] this lease has no security deposit configured")
    check("occupancy rows opened", occ.exists())

    print("\n  Generated charges (first 8, by due date):")
    for c in charges.order_by("due_date", "entry_type")[:8]:
        print(f"    {c.due_date}  {c.get_entry_type_display():18} ${c.amount}")

    print("\n  Occupancy rows:")
    for o in occ:
        room = o.room.name if o.room else "-"
        print(f"    tenant={o.tenant_id}  room={room}  from {o.move_in}")

    transaction.savepoint_rollback(sid)

print("\nRolled back - nothing was persisted; your data is unchanged.")
passed = sum(1 for _, ok in RESULTS if ok)
print(f"\n=== {passed}/{len(RESULTS)} checks passed ===")
if passed != len(RESULTS):
    print("FAILURES:", [n for n, ok in RESULTS if not ok])
