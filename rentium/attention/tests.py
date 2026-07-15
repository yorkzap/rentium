"""Action Center tests: sources compute from real models, ordering holds,
and the endpoint speaks the exact contract the frontend types expect.
Fixtures (landlord, tenant, bc_lease) live in rentium/conftest.py."""

from datetime import date, timedelta

import pytest
from rest_framework.test import APIClient

from rentium.ledger import services as ledger_services
from rentium.ledger.models import EntryType

from .service import (
    _expiring_leases,
    _missing_move_in_inspections,
    _overdue_charges,
    compute_attention,
)

pytestmark = pytest.mark.django_db


# ------------------------------------------------- BC move-in inspection
def test_bc_lease_without_inspection_yields_urgent_item(bc_lease, landlord):
    items = _missing_move_in_inspections(landlord)
    assert len(items) == 1
    item = items[0]
    assert item.severity == "urgent"
    assert item.source == "inspection"
    assert item.key == f"inspection.move_in.lease:{bc_lease.pk}"
    assert str(bc_lease.pk) in item.url


def test_exempt_shared_with_landlord_lease_yields_no_item(landlord):
    # RTA s.4(c): owner shares kitchen/bath -> the Act (and its inspection
    # requirement) doesn't govern this tenancy. Sharing is only valid on
    # roommate agreements, which in turn require a ROOM property.
    from datetime import date as _date

    from rentium.leases.models import Lease
    from rentium.properties.models import Property

    room = Property.objects.create(
        landlord=landlord,
        name="Fern Rd Room 1",
        address="9 Fern Rd",
        city="Victoria",
        province="BC",
        postal_code="V8V 1V1",
        property_category=Property.PropertyCategory.ROOM,
    )
    Lease.objects.create(
        landlord=landlord,
        property=room,
        lease_type=Lease.LeaseType.BC_ROOMMATE_AGREEMENT,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=_date.today(),
        is_month_to_month=True,
        total_rent="600.00",
        common_space_shared_with=["LANDLORD"],
    )
    assert _missing_move_in_inspections(landlord) == []


# ------------------------------------------------------- expiring leases
def test_fixed_term_ending_soon_yields_info_item(bc_lease, landlord):
    bc_lease.is_month_to_month = False
    bc_lease.end_date = date.today() + timedelta(days=30)
    bc_lease.save(update_fields=["is_month_to_month", "end_date"])
    items = _expiring_leases(landlord)
    assert [i.severity for i in items] == ["info"]
    assert items[0].due_date == bc_lease.end_date


def test_month_to_month_never_expires(bc_lease, landlord):
    # The fixture lease is month-to-month with no end date (the model
    # forbids M2M + end_date) — it must never show up as "expiring".
    assert _expiring_leases(landlord) == []


# -------------------------------------------------------- overdue money
def test_overdue_charge_yields_urgent_item(bc_lease, landlord):
    ledger_services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_lease.property,
        amount="850.00",
        due_date=date.today() - timedelta(days=5),
        entry_type=EntryType.RENT_CHARGE,
        description="Rent",
    )
    items = _overdue_charges(landlord)
    assert len(items) == 1
    assert items[0].severity == "urgent"
    assert "850" in items[0].title


# ------------------------------------------------------------- ordering
def test_compute_attention_orders_urgent_first(bc_lease, landlord):
    # expiring (info) + missing inspection (urgent) on the same lease
    bc_lease.is_month_to_month = False
    bc_lease.end_date = date.today() + timedelta(days=30)
    bc_lease.save(update_fields=["is_month_to_month", "end_date"])
    severities = [i.severity for i in compute_attention(landlord)]
    assert severities == sorted(
        severities, key=lambda s: {"urgent": 0, "soon": 1, "info": 2}[s]
    )
    assert severities[0] == "urgent"


# ------------------------------------------------------------- endpoint
def test_attention_endpoint_contract(bc_lease, landlord):
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    res = client.get("/api/attention/")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) >= 1
    first = items[0]
    # Exact field set the frontend's ActionItem type expects.
    assert set(first) == {
        "key", "severity", "title", "detail", "url", "due_date", "source",
    }


def test_attention_endpoint_rejects_tenants(tenant):
    client = APIClient()
    client.force_authenticate(user=tenant.user)
    assert client.get("/api/attention/").status_code == 403
