"""Phase B summary tests: deposits show up as money movement without ever
counting as income, and an empty month points at the next real charge."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from . import services
from .models import EntryType

pytestmark = pytest.mark.django_db


def _post_deposit_and_pay(landlord, lease, amount="425.00"):
    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=lease,
        property=lease.property,
        amount=amount,
        due_date=date.today(),
        entry_type=EntryType.DEPOSIT_CHARGE,
        description="Security deposit",
    )
    services.record_payment(charge=charge, amount=amount, payment_method="ETRANSFER")
    return charge


# The $425 regression: a collected deposit must be visible as money that
# hit the bank while staying out of income.
def test_deposit_collected_is_reported_but_not_income(bc_lease, landlord):
    _post_deposit_and_pay(landlord, bc_lease)

    start = date.today().replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    assert services.deposits_collected_between(landlord, start, end) == Decimal(
        "425.00"
    )

    client = APIClient()
    client.force_authenticate(user=landlord.user)
    data = client.get("/api/ledger/summary/?months=1").json()

    current = data["monthly"][-1]
    assert current["deposits_collected"] == "425.00"
    assert current["collected_income"] == "0.00"  # unchanged: not income
    assert data["collected_this_month_total"] == "425.00"
    assert data["deposits_held"] == "425.00"


def test_next_charge_points_at_future_rent(bc_lease, landlord):
    due = date.today() + timedelta(days=20)
    services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_lease.property,
        amount="850.00",
        due_date=due,
        entry_type=EntryType.RENT_CHARGE,
        description="August rent",
    )

    nxt = services.next_upcoming_charge(landlord)
    assert nxt is not None
    assert nxt["due_date"] == due.isoformat()
    assert nxt["amount"] == "850.00"
    assert nxt["entry_type"] == "RENT_CHARGE"
    assert nxt["property_name"] == bc_lease.property.name

    client = APIClient()
    client.force_authenticate(user=landlord.user)
    data = client.get("/api/ledger/summary/?months=1").json()
    assert data["next_charge"]["amount"] == "850.00"


def test_next_charge_none_when_nothing_upcoming(landlord):
    assert services.next_upcoming_charge(landlord) is None


# ------------------------------------------- damage claims vs expected income
# A FEE_CHARGE means two unrelated things. A late fee is ordinary income and
# must keep counting; a damage-recovery claim is contested and settles at
# move-out. Only the damage claim carries a work_order.
def _work_order(landlord, prop):
    from rentium.maintenance.models import WorkOrder

    return WorkOrder.objects.create(
        property=prop,
        title="Shower leak + hot water knob replacement",
        category=WorkOrder.Category.PLUMBING,
    )


def _damage_fee(landlord, lease, prop, work_order, amount="19.78"):
    from rentium.ledger import services

    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=lease,
        property=prop,
        amount=amount,
        due_date=date.today().replace(day=1),
        entry_type=EntryType.FEE_CHARGE,
        description="Damage recovery: shower leak",
        work_order=work_order,
    )
    return charge


def _late_fee(landlord, lease, prop, amount="25.00"):
    from rentium.ledger import services

    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=lease,
        property=prop,
        amount=amount,
        due_date=date.today().replace(day=1),
        entry_type=EntryType.FEE_CHARGE,
        description="Late fee",
    )
    return charge


@pytest.mark.django_db
def test_damage_claim_is_excluded_from_expected_income(landlord, bc_lease, bc_property):
    from rentium.ledger.models import LedgerEntry

    _damage_fee(landlord, bc_lease, bc_property, _work_order(landlord, bc_property))
    live = LedgerEntry.objects.filter(landlord=landlord).not_voided()

    assert live.damage_claims().count() == 1
    assert live.expected_income().filter(entry_type=EntryType.FEE_CHARGE).count() == 0


@pytest.mark.django_db
def test_a_late_fee_is_still_expected_income(landlord, bc_lease, bc_property):
    """The narrow fix must not quietly stop counting real fee income."""
    from rentium.ledger.models import LedgerEntry

    _late_fee(landlord, bc_lease, bc_property)
    live = LedgerEntry.objects.filter(landlord=landlord).not_voided()

    assert live.damage_claims().count() == 0
    assert live.expected_income().filter(entry_type=EntryType.FEE_CHARGE).count() == 1


@pytest.mark.django_db
def test_a_damage_claim_is_still_a_claim_against_the_deposit(
    landlord, bc_lease, bc_property
):
    """Excluding it from expected income must not make it disappear — it is
    still owed, and deposit_position must still report it with its routes."""
    from rentium.ledger import services

    _damage_fee(landlord, bc_lease, bc_property, _work_order(landlord, bc_property))
    position = services.deposit_position(landlord, lease=bc_lease)

    assert Decimal(position["claimed"]) >= Decimal("19.78")
    assert position["claims"]
    assert position["lawful_routes"]
