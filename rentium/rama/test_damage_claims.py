"""Damage -> responsible tenant -> claim -> deposit position.

The chain a landlord actually needs: "the shower knob was broken by the tenant
in Room C, I want that off their deposit later."

The important part is what it deliberately does NOT do. Under the BC RTA a
landlord may keep deposit money only with the tenant's WRITTEN agreement, or by
applying to the RTB within 15 days of the later of the tenancy ending and
receiving the forwarding address. Getting that wrong loses the claim AND makes
double the deposit payable. So damage raises a CLAIM the tenant owes; the
deposit stays a separate liability, and nothing nets one off the other.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from rentium.ledger.models import LedgerEntry
from rentium.ledger.services import deposit_position, post_charge, record_payment
from rentium.maintenance.models import WorkOrder
from rentium.properties.models import Property
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit
from rentium.rama import registry

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenancy(landlord, tenant):
    from rentium.leases.models import Lease

    holding = PropertyHolding.objects.create(
        landlord=landlord, name="950 McKenzie Ave",
        address="950 McKenzie Ave", city="Victoria",
    )
    unit = PropertyUnit.objects.create(
        landlord=landlord, holding=holding, name="Basement",
        rental_mode=PropertyUnit.RentalMode.BY_ROOM,
    )
    room = Property.objects.create(
        landlord=landlord, holding=holding, unit=unit, name="Room C",
        address="950 McKenzie Ave", city="Victoria", province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    lease = Lease.objects.create(
        landlord=landlord, property=room,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE, start_date=date.today(),
        is_month_to_month=True, total_rent="850.00",
    )
    lease.lease_tenants.create(tenant=tenant, rent_amount="850.00")
    return {"unit": unit, "room": room, "lease": lease, "tenant": tenant}


def _job(landlord, tenancy, *, cost="19.78", complete=True):
    created = registry.execute(
        "create_work_order",
        {"property_query": "McKenzie Basement", "title": "Shower knob broken",
         "priority": "HIGH", "category": "PLUMBING", "confirm": "yes"},
        landlord=landlord,
    )
    wo = WorkOrder.objects.get(pk=created["work_order"]["id"])
    if complete:
        registry.execute(
            "complete_work_order",
            {"work_order_id": str(wo.pk), "cost": cost, "confirm": "yes"},
            landlord=landlord,
        )
        wo.refresh_from_db()
    return wo


def _attribute(landlord, tenancy, **kw):
    return registry.execute(
        "attribute_work_order",
        {"title_query": "Shower knob", "tenant": tenancy["tenant"].user.name,
         "confirm": "yes", **kw},
        landlord=landlord,
    )


# ------------------------------------------------------------ attribution
def test_damage_can_be_pinned_to_the_tenant_who_caused_it(landlord, tenancy):
    _job(landlord, tenancy)
    result = _attribute(landlord, tenancy, chargeable="yes")

    wo = WorkOrder.objects.get()
    assert wo.responsible_tenant == tenancy["tenant"]
    assert wo.tenant_chargeable is True
    assert result["responsible_tenant"] == tenancy["tenant"].user.name


def test_blame_after_the_job_closed_still_raises_the_claim(landlord, tenancy):
    """The common case: fix it, close it, work out who broke it later.
    COMPLETED is terminal, so a claim that only rode on completion could never
    be raised at all."""
    wo = _job(landlord, tenancy)
    assert wo.status == WorkOrder.Status.COMPLETED

    result = _attribute(landlord, tenancy, chargeable="yes")
    assert result["tenant_claim_id"]
    assert LedgerEntry.objects.filter(entry_type="FEE_CHARGE").count() == 1


def test_attributing_twice_does_not_charge_twice(landlord, tenancy):
    _job(landlord, tenancy)
    _attribute(landlord, tenancy, chargeable="yes")
    _attribute(landlord, tenancy, chargeable="yes")

    assert LedgerEntry.objects.filter(entry_type="FEE_CHARGE").count() == 1


def test_naming_someone_without_charging_them_raises_nothing(landlord, tenancy):
    """Recording who caused it is useful on its own — it is evidence. Money is
    a separate decision."""
    _job(landlord, tenancy)
    _attribute(landlord, tenancy)

    assert WorkOrder.objects.get().responsible_tenant == tenancy["tenant"]
    assert not LedgerEntry.objects.filter(entry_type="FEE_CHARGE").exists()


def test_chargeable_without_a_name_is_refused(landlord, tenancy):
    _job(landlord, tenancy)
    out = registry.execute(
        "attribute_work_order",
        {"title_query": "Shower knob", "chargeable": "yes", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in out


def test_an_unknown_tenant_is_never_guessed(landlord, tenancy):
    _job(landlord, tenancy)
    out = registry.execute(
        "attribute_work_order",
        {"title_query": "Shower knob", "tenant": "Nobody At All",
         "chargeable": "yes", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in out
    assert not LedgerEntry.objects.filter(entry_type="FEE_CHARGE").exists()


def test_the_claim_lands_on_the_lease_whose_deposit_it_may_touch(
    landlord, tenancy
):
    """A shared-space job carries no lease of its own. Without the fallback the
    claim exists but never appears in the deposit position, and the gap is
    discovered at move-out."""
    _job(landlord, tenancy)
    _attribute(landlord, tenancy, chargeable="yes")

    claim = LedgerEntry.objects.get(entry_type="FEE_CHARGE")
    assert claim.lease == tenancy["lease"]
    assert claim.work_order is not None


# -------------------------------------------------------- deposit position
def test_a_damage_claim_shows_against_the_deposit(landlord, tenancy):
    charge, _ = post_charge(
        landlord=landlord, tenant=tenancy["tenant"], lease=tenancy["lease"],
        amount="425.00", due_date=date.today(),
        entry_type="DEPOSIT_CHARGE", description="Security deposit",
    )
    record_payment(charge=charge, amount="425.00", payment_method="ETRANSFER")

    _job(landlord, tenancy)
    _attribute(landlord, tenancy, chargeable="yes")

    position = deposit_position(landlord, lease=tenancy["lease"])
    assert Decimal(position["deposit_held"]) == Decimal("425.00")
    damage = [c for c in position["claims"] if c["is_damage"]]
    assert len(damage) == 1
    assert Decimal(damage[0]["amount"]) == Decimal("19.78")


def test_the_position_never_presents_itself_as_a_deduction(landlord, tenancy):
    """The number a landlord must NOT be handed is "you may keep $19.78".
    Every response carries the two lawful routes and the double-penalty
    warning, so the field name alone cannot be mistaken for permission."""
    position = deposit_position(landlord, lease=tenancy["lease"])

    assert "returnable_if_all_claims_agreed" in position
    assert "deposit_deduction" not in position
    assert len(position["lawful_routes"]) == 2
    assert "in writing" in " ".join(position["lawful_routes"]).lower()
    assert "double" in position["warning"].lower()


def test_the_deadline_needs_a_forwarding_address_not_just_an_end_date(
    landlord, tenancy
):
    """The clock starts on the LATER of the tenancy ending and the forwarding
    address arriving in writing. Reporting a deadline from the end date alone
    would name one that has not started — worse than reporting none, because
    the landlord would act on it."""
    lease = tenancy["lease"]
    lease.move_out_date = date.today()
    lease.save()

    position = deposit_position(landlord, lease=lease)
    assert position["tenancy_ended"] == date.today().isoformat()
    assert position["claim_deadline"] is None
    assert "forwarding address" in (position["clock_note"] or "")


def test_the_deadline_is_computed_once_the_forwarding_address_arrives(
    landlord, tenancy
):
    from rentium.leases.moveout import MoveOutRequest

    lease = tenancy["lease"]
    lease.move_out_date = date.today() - timedelta(days=2)
    lease.save()
    request = MoveOutRequest.objects.create(
        lease=lease,
        initiated_by=MoveOutRequest.InitiatedBy.TENANT,
        kind=MoveOutRequest.Kind.TENANT_NOTICE,
        requested_end_date=lease.move_out_date,
        effective_end_date=lease.move_out_date,
        forwarding_address="12 Elsewhere Rd, Victoria BC",
        forwarding_address_received_on=date.today(),
    )

    # Later of (ended 2 days ago, address today) -> today + 15.
    assert request.deposit_deadline == date.today() + timedelta(days=15)
    assert request.days_left_to_settle == 15
    assert request.deposit_status()["overdue"] is False

    position = deposit_position(landlord, lease=lease)
    assert position["claim_deadline"] == (
        date.today() + timedelta(days=15)
    ).isoformat()


def test_an_unsettled_deposit_past_the_deadline_reads_as_overdue(
    landlord, tenancy
):
    from rentium.leases.moveout import MoveOutRequest

    lease = tenancy["lease"]
    ended = date.today() - timedelta(days=30)
    request = MoveOutRequest.objects.create(
        lease=lease,
        initiated_by=MoveOutRequest.InitiatedBy.TENANT,
        kind=MoveOutRequest.Kind.TENANT_NOTICE,
        requested_end_date=ended,
        effective_end_date=ended,
        forwarding_address_received_on=ended,
    )
    status = request.deposit_status()

    assert status["overdue"] is True
    assert "double" in status["if_missed"].lower()

    request.deposit_settlement = MoveOutRequest.DepositSettlement.RTB_APPLIED
    request.save()
    assert request.deposit_status()["overdue"] is False


def test_no_deadline_while_the_tenancy_is_running(landlord, tenancy):
    position = deposit_position(landlord, lease=tenancy["lease"])
    assert position["claim_deadline"] is None


def test_deposit_held_counts_money_actually_received(landlord, tenancy):
    """An unpaid deposit charge is not deposit money in hand."""
    post_charge(
        landlord=landlord, tenant=tenancy["tenant"], lease=tenancy["lease"],
        amount="425.00", due_date=date.today(),
        entry_type="DEPOSIT_CHARGE", description="Security deposit",
    )
    position = deposit_position(landlord, lease=tenancy["lease"])
    assert Decimal(position["deposit_held"]) == Decimal("0.00")
