from datetime import date
from django.db import transaction
from rentium.leases.models import Lease
from rentium.ledger.billing import generate_rent_charges_for_lease, _effective_start
from rentium.ledger.models import LedgerEntry, EntryType

lease = (
    Lease.objects.filter(lease_tenants__tenant__isnull=False, lease_tenants__has_signed=True)
    .distinct().first()
)
lt = lease.lease_tenants.filter(tenant__isnull=False, declined=False).first()
start = _effective_start(lt)
horizon = max((start - date.today()).days + 90, 45)
print(f"lease effective start ~{start}; using a {horizon}-day horizon to reach it")

with transaction.atomic():
    sid = transaction.savepoint()
    lease.status = Lease.LeaseStatus.ACTIVE
    lease.save(update_fields=["status", "updated_at"])
    n = generate_rent_charges_for_lease(lease, horizon_days=horizon)
    rents = LedgerEntry.objects.filter(
        lease=lease, entry_type=EntryType.RENT_CHARGE
    ).order_by("due_date")
    print("rent charges created:", n)
    for c in rents[:8]:
        print(f"   {c.due_date}  ${c.amount}")
    transaction.savepoint_rollback(sid)
print("rolled back - nothing persisted")
