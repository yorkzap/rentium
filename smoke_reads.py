"""
Smoke test for the read-endpoint backends (notifications, messaging, agenda).

    python manage.py shell < smoke_reads.py

Runs inside a rolled-back transaction where it can, and cleans up what it
can't, so your data is left as-is. Prints PASS/FAIL per check.
"""

from datetime import date, timedelta

from django.db import transaction

RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


print("\n=== Read-endpoints smoke test ===\n")

# ---------------------------------------------------------------- fixtures
from rentium.leases.models import Lease
from rentium.users.models import LandlordProfile, TenantProfile

lease = (
    Lease.objects.filter(lease_tenants__tenant__isnull=False)
    .select_related("landlord")
    .distinct()
    .first()
)
if not lease:
    print("No lease with a tenant found. Seed data first.")
    raise SystemExit

landlord = lease.landlord
lt = lease.lease_tenants.filter(tenant__isnull=False).first()
tenant = lt.tenant
landlord_user = getattr(landlord, "user", None)
tenant_user = getattr(tenant, "user", None)
print(f"landlord={landlord.pk} tenant={tenant.pk} lease={lease.pk}\n")


# ---------------------------------------------------- 1. notification fan-out
print("Notifications")
from rentium.events.models import DomainEvent, Notification
from rentium.events.notify import fan_out_to_notifications

with transaction.atomic():
    sid = transaction.savepoint()

    # Simulate an event the dispatcher would process.
    ev = DomainEvent.objects.create(
        event_type="maintenance.created",
        property_id=lease.property_id,
        lease_id=lease.pk,
        payload={"title": "Test leak", "rta_emergency": False, "priority": "HIGH"},
    )
    fan_out_to_notifications(ev)          # what the Celery dispatcher calls
    fan_out_to_notifications(ev)          # again — must NOT duplicate

    notes = Notification.objects.filter(event=ev)
    check("maintenance.created notifies the landlord", notes.filter(recipient=landlord_user).exists() if landlord_user else False)
    check("fan-out is idempotent (no dupes on replay)", notes.count() == notes.values("recipient").distinct().count())

    # A tenant-facing event
    ev2 = DomainEvent.objects.create(
        event_type="ledger.charge_due_soon", lease_id=lease.pk,
        payload={"amount_outstanding": "500.00", "due_date": str(date.today())},
    )
    fan_out_to_notifications(ev2)
    check("charge_due_soon notifies the tenant", Notification.objects.filter(event=ev2, recipient=tenant_user).exists() if tenant_user else False)

    transaction.savepoint_rollback(sid)


# --------------------------------------------------------- 2. messaging
print("\nMessaging")
from rentium.messaging.models import Conversation, Message
from rentium.messaging.services import send_message

with transaction.atomic():
    sid = transaction.savepoint()

    convo, _ = Conversation.objects.get_or_create(landlord=landlord, tenant=tenant, lease=lease)
    before_events = DomainEvent.objects.count()

    if landlord_user:
        msg = send_message(convo, landlord_user, "Hi, the plumber comes tomorrow 2-4pm.")
        check("message created", Message.objects.filter(pk=msg.pk).exists())
        check("thread bumped", convo.messages.count() >= 1)
        check("message.created event published", DomainEvent.objects.count() > before_events)
        ev = DomainEvent.objects.filter(event_type="message.created").order_by("-created_at").first()
        check("event targets the tenant", tenant_user and tenant_user.pk in (ev.payload.get("recipient_ids") or []))
    else:
        print("  [skip] landlord has no user account")

    transaction.savepoint_rollback(sid)


# --------------------------------------------------------- 3. agenda feed
print("\nAgenda / calendar feed")
from rentium.agenda.api.views import agenda_feed
from rest_framework.test import APIRequestFactory, force_authenticate

if landlord_user:
    factory = APIRequestFactory()
    start = date.today() - timedelta(days=365)
    end = date.today() + timedelta(days=730)
    req = factory.get(f"/api/agenda/?start={start}&end={end}")
    force_authenticate(req, user=landlord_user)
    resp = agenda_feed(req)
    check("agenda feed returns 200", resp.status_code == 200)
    items = resp.data.get("items", [])
    check("agenda feed returns a list", isinstance(items, list))
    types = {i["type"] for i in items}
    print(f"    item types present: {sorted(types) or 'none in range'}")
    print(f"    total items: {len(items)}")
else:
    print("  [skip] landlord has no user account")


# --------------------------------------------------------- summary
passed = sum(1 for _, ok in RESULTS if ok)
print(f"\n=== {passed}/{len(RESULTS)} checks passed ===")
if passed != len(RESULTS):
    print("FAILURES:", [n for n, ok in RESULTS if not ok])
