"""Phase B summary tests: deposits show up as money movement without ever
counting as income, and an empty month points at the next real charge."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db.models import Sum
from rest_framework.test import APIClient

from . import services
from .models import INCOME_CHARGE_TYPES, EntryType

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


def test_joint_charge_serializes_every_non_declined_lease_tenant(
    landlord, bc_lease,
):
    from rentium.leases.models import LeaseTenant
    from rentium.ledger.api.views import LedgerEntrySerializer
    from rentium.ledger.models import LedgerEntry

    LeaseTenant.objects.create(
        lease=bc_lease,
        invited_name="Aishwarya Chenthamara",
        invited_email="aishwarya@example.com",
        rent_amount=Decimal("0.00"),
    )
    LeaseTenant.objects.create(
        lease=bc_lease,
        invited_name="Naveen Prasanth Singaravel",
        invited_email="naveen@example.com",
        rent_amount=Decimal("850.00"),
    )
    LeaseTenant.objects.create(
        lease=bc_lease,
        invited_name="Declined Person",
        invited_email="declined@example.com",
        rent_amount=Decimal("0.00"),
        declined=True,
    )
    charge = LedgerEntry.objects.create(
        landlord=landlord,
        lease=bc_lease,
        property=bc_lease.property,
        tenant=None,
        entry_type=EntryType.RENT_CHARGE,
        amount=Decimal("400.00"),
        due_date=date.today(),
        effective_date=date.today(),
        description="Monthly household rent",
    )

    data = LedgerEntrySerializer(charge).data

    assert data["tenant_name"] is None
    assert data["tenant_names"] == [
        "Aishwarya Chenthamara",
        "Naveen Prasanth Singaravel",
    ]
    assert data["is_joint"] is True


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


# ---------------------------------------------------------------------------
# The annotation contract: only a charge is a receivable, so only a charge has
# a balance. api/views.py has always documented "charges only; null otherwise"
# for settled_amount / outstanding; nothing tested it, and with_settlement()
# quietly annotated every row — so the Financial feed showed a settled expense
# as "Paid … $31.45 left" and a REVERSAL as a live $19.78 balance beside the
# entry it had just voided. These tests are what keep the docstring true.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_outstanding_is_null_on_every_non_charge_type(landlord, bc_lease, bc_property):
    from rentium.ledger.models import CHARGE_TYPES, LedgerEntry

    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_property,
        amount="850.00",
        due_date=date.today(),
        entry_type=EntryType.RENT_CHARGE,
        description="Monthly rent",
    )
    services.record_payment(charge=charge, amount="100.00", payment_method="ETRANSFER")
    expense, _ = services.post_expense(
        landlord=landlord,
        amount="19.78",
        category="MAINTENANCE",
        description="Hot water knob replacement",
        property=bc_property,
    )
    services.void_entry(expense, reason="Shared-space repair — belongs to the address")

    rows = LedgerEntry.objects.filter(landlord=landlord).with_settlement()
    non_charges = [r for r in rows if r.entry_type not in CHARGE_TYPES]

    # PAYMENT, EXPENSE and REVERSAL are all present, and none of them owes anything.
    assert {r.entry_type for r in non_charges} == {
        EntryType.PAYMENT,
        EntryType.EXPENSE,
        EntryType.REVERSAL,
    }
    for row in non_charges:
        assert row.outstanding is None, f"{row.entry_type} reported a balance"
        assert row.settled_amount is None, f"{row.entry_type} reported a settlement"


@pytest.mark.django_db
def test_charges_still_report_their_balance(landlord, bc_lease, bc_property):
    """The guard must not cost charges the annotation they exist for."""
    from rentium.ledger.models import LedgerEntry

    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_property,
        amount="850.00",
        due_date=date.today(),
        entry_type=EntryType.RENT_CHARGE,
        description="Monthly rent",
    )
    services.record_payment(charge=charge, amount="100.00", payment_method="ETRANSFER")

    row = LedgerEntry.objects.with_settlement().get(pk=charge.pk)
    assert row.settled_amount == Decimal("100.00")
    assert row.outstanding == Decimal("750.00")


@pytest.mark.django_db
def test_a_voided_charge_still_reports_zero_not_null(landlord, bc_lease, bc_property):
    """Ordering of the Case branches: a voided CHARGE is settled at 0.00, while
    a voided EXPENSE is not a receivable at all and must stay NULL."""
    from rentium.ledger.models import LedgerEntry

    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_property,
        amount="425.00",
        due_date=date.today(),
        entry_type=EntryType.DEPOSIT_CHARGE,
        description="Security deposit",
    )
    services.void_entry(charge, reason="Raised against the wrong lease")

    row = LedgerEntry.objects.with_settlement().get(pk=charge.pk)
    assert row.outstanding == Decimal("0.00")
    assert row.is_voided is True


@pytest.mark.django_db
def test_an_expense_never_reports_money_left(landlord, bc_property):
    """The exact reported symptom: a row reading 'Paid' and '$31.45 left'."""
    from rentium.ledger.models import LedgerEntry

    expense, _ = services.post_expense(
        landlord=landlord,
        amount="31.45",
        category="MAINTENANCE",
        description="Garden mulching",
        property=bc_property,
        paid_on=date.today(),
    )

    row = LedgerEntry.objects.with_settlement().get(pk=expense.pk)
    assert row.bank_status == "PAID"
    assert row.outstanding is None


@pytest.mark.django_db
def test_summary_totals_are_unchanged_by_the_null_annotation(
    landlord, bc_lease, bc_property
):
    """Regression guard for the consumers that filter outstanding__gt=0: NULL
    must drop out of that comparison exactly as the old 0.00 did, so the
    Outstanding / Overdue tiles do not move."""
    from rentium.ledger.models import LedgerEntry

    services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_property,
        amount="850.00",
        due_date=date.today() - timedelta(days=3),
        entry_type=EntryType.RENT_CHARGE,
        description="Monthly rent",
    )
    services.post_expense(
        landlord=landlord,
        amount="61.23",
        category="MAINTENANCE",
        description="Noise that must not land in outstanding",
        property=bc_property,
    )

    open_charges = (
        LedgerEntry.objects.filter(landlord=landlord)
        .with_settlement()
        .filter(
            entry_type__in=INCOME_CHARGE_TYPES,
            reversed_by__isnull=True,
            due_date__lte=date.today(),
            outstanding__gt=0,
        )
    )
    assert open_charges.count() == 1
    assert open_charges.aggregate(t=Sum("outstanding"))["t"] == Decimal("850.00")


# ---------------------------------------------------------------------------
# A void is one event, not two rows. The UI cannot pair the REVERSAL with its
# target client-side — the reversal is dated the day of the correction, so with
# -effective_date ordering the two can be months apart, and an entry_type
# filter drops the reversal from the response entirely. So the correction is
# carried on the entry it voided.
# ---------------------------------------------------------------------------
def _entries(landlord):
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    response = client.get("/api/ledger/entries/")
    assert response.status_code == 200
    body = response.json()
    return body["results"] if isinstance(body, dict) else body


@pytest.mark.django_db
def test_a_voided_entry_carries_when_and_why(landlord, bc_property):
    reason = "Shared-space repair: the shower serves Rooms C, D and F"
    expense, _ = services.post_expense(
        landlord=landlord,
        amount="19.78",
        category="MAINTENANCE",
        description="Hot water knob replacement",
        property=bc_property,
    )
    reversal = services.void_entry(expense, reason=reason)

    rows = {r["id"]: r for r in _entries(landlord)}
    target = rows[str(expense.id)]

    assert target["voided"] is True
    assert target["voided_on"] == reversal.effective_date.isoformat()
    assert target["void_reason"] == reason
    # And the reversal can still name what it undid, for when it is shown.
    assert rows[str(reversal.id)]["reverses_effective_date"] == (
        expense.effective_date.isoformat()
    )


@pytest.mark.django_db
def test_the_void_fields_survive_a_reversal_posted_much_later(
    landlord, bc_property
):
    """The reversal is dated today, the original may be months old — which is
    exactly why the pairing cannot be left to row adjacency in the feed."""
    expense, _ = services.post_expense(
        landlord=landlord,
        amount="19.78",
        category="MAINTENANCE",
        description="Hot water knob replacement",
        property=bc_property,
        incurred_date=date.today() - timedelta(days=120),
    )
    services.void_entry(expense, reason="Booked to the wrong room")

    target = next(r for r in _entries(landlord) if r["id"] == str(expense.id))
    assert target["voided_on"] == date.today().isoformat()
    assert target["void_reason"] == "Booked to the wrong room"


@pytest.mark.django_db
def test_a_live_entry_has_no_void_fields(landlord, bc_property):
    """The hasattr guard: a reverse OneToOne raises rather than returning None."""
    services.post_expense(
        landlord=landlord,
        amount="31.45",
        category="MAINTENANCE",
        description="Garden mulching",
        property=bc_property,
    )

    row = next(iter(_entries(landlord)))
    assert row["voided"] is False
    assert row["voided_on"] is None
    assert row["void_reason"] is None
    assert row["reverses_effective_date"] is None


# ---------------------------------------------------------------------------
# The tiles and the rows must describe the same money. A $425 deposit charge
# was badged "overdue" in the ledger feed while the Outstanding tile read
# $19.78 and the Overdue tile read 1 — because both tiles counted income only,
# and a deposit is (correctly) not income. The classification stays; what
# changes is that the deposit is now disclosed instead of silently dropped.
# ---------------------------------------------------------------------------
def _summary(landlord):
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    return client.get("/api/ledger/summary/?months=1").json()


@pytest.mark.django_db
def test_an_overdue_deposit_is_disclosed_without_becoming_income(
    landlord, bc_lease, bc_property
):
    services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_property,
        amount="425.00",
        due_date=date.today() - timedelta(days=7),
        entry_type=EntryType.DEPOSIT_CHARGE,
        description="Security deposit — due on signing",
    )

    data = _summary(landlord)

    # Income is untouched: a deposit is a refundable liability, not earnings.
    assert data["outstanding_total"] == "0.00"
    assert data["overdue_count"] == 0
    # But it is owed, and now says so.
    assert data["deposits_outstanding"] == "425.00"
    assert data["deposits_overdue_count"] == 1
    assert data["owed_total"] == "425.00"
    assert data["owed_overdue_count"] == 1


@pytest.mark.django_db
def test_a_damage_claim_gets_its_own_bucket(landlord, bc_lease, bc_property):
    """It counts as income (it always did) but is not rent, so the breakdown
    can explain a total that expected_income never showed."""
    _damage_fee(landlord, bc_lease, bc_property, _work_order(landlord, bc_property))

    data = _summary(landlord)

    assert data["damage_claims_outstanding"] == "19.78"
    assert data["rent_outstanding"] == "0.00"
    assert data["outstanding_total"] == "19.78"  # unchanged behaviour
    assert data["owed_total"] == "19.78"


@pytest.mark.django_db
def test_the_owed_tile_equals_the_charge_rows_it_sits_above(
    landlord, bc_lease, bc_property
):
    """The whole point: the headline number and the rows underneath it are
    provably the same set."""
    services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_property,
        amount="425.00",
        due_date=date.today() - timedelta(days=7),
        entry_type=EntryType.DEPOSIT_CHARGE,
        description="Security deposit",
    )
    _damage_fee(landlord, bc_lease, bc_property, _work_order(landlord, bc_property))

    client = APIClient()
    client.force_authenticate(user=landlord.user)
    rows = client.get("/api/ledger/entries/charges/").json()
    rows = rows["results"] if isinstance(rows, dict) else rows

    owed_in_rows = sum(
        Decimal(r["outstanding"])
        for r in rows
        if not r["voided"] and Decimal(r["outstanding"] or "0") > 0
    )
    assert Decimal(_summary(landlord)["owed_total"]) == owed_in_rows == Decimal("444.78")


def test_security_and_cleaning_deposits_return_as_separate_liabilities(
    landlord, bc_lease
):
    charges = []
    for description, kind in (
        ("Security deposit", "security_deposit"),
        ("Cleaning deposit", "cleaning_deposit_lease"),
    ):
        charge, _ = services.post_charge(
            landlord=landlord,
            tenant=None,
            lease=bc_lease,
            property=bc_lease.property,
            amount="200.00",
            due_date=date.today(),
            entry_type=EntryType.DEPOSIT_CHARGE,
            description=description,
            metadata={"kind": kind},
        )
        services.record_payment(
            charge=charge,
            amount="200.00",
            payment_method="ETRANSFER",
        )
        charges.append(charge)

    assert services.deposits_held(landlord) == Decimal("400.00")
    returned = services.return_refundable_deposits(
        landlord=landlord,
        lease=bc_lease,
        payment_method="ETRANSFER",
    )

    assert len(returned) == 2
    assert {entry.description for entry in returned} == {
        "Security deposit returned",
        "Cleaning deposit returned",
    }
    assert {entry.amount for entry in returned} == {Decimal("200.00")}
    assert all(entry.metadata["returned_separately"] for entry in returned)
    assert {
        entry.metadata["source_charge_id"] for entry in returned
    } == {str(charge.pk) for charge in charges}
    assert services.deposits_held(landlord) == Decimal("0.00")


def test_lease_without_cleaning_deposit_returns_only_security(landlord, bc_lease):
    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_lease.property,
        amount="200.00",
        due_date=date.today(),
        entry_type=EntryType.DEPOSIT_CHARGE,
        description="Security deposit",
        metadata={"kind": "security_deposit"},
    )
    services.record_payment(
        charge=charge,
        amount="200.00",
        payment_method="ETRANSFER",
    )

    returned = services.return_refundable_deposits(
        landlord=landlord,
        lease=bc_lease,
        payment_method="ETRANSFER",
    )

    assert [entry.description for entry in returned] == [
        "Security deposit returned"
    ]


def test_a_paid_cleaning_deposit_stamps_its_receipt_date_on_the_lease(
    landlord, bc_lease
):
    """The cleaning deposit is refundable, so its receipt starts the same
    15-day clock the security deposit's does. Before this it had only a
    boolean, and the agreement printed 'Not yet received' forever."""
    from .billing import stamp_deposit_received

    paid_on = date.today() - timedelta(days=3)
    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_lease.property,
        amount="200.00",
        due_date=date.today(),
        entry_type=EntryType.DEPOSIT_CHARGE,
        description="Cleaning deposit",
        metadata={"kind": "cleaning_deposit_lease"},
    )
    services.record_payment(
        charge=charge,
        amount="200.00",
        payment_method="ETRANSFER",
        payment_date=paid_on,
    )

    assert stamp_deposit_received(charge) is True
    bc_lease.refresh_from_db()
    assert bc_lease.cleaning_deposit_received_date == paid_on
    # The old behaviour — flipping the per-tenant paid flag — still happens.
    assert all(
        lt.cleaning_deposit_paid
        for lt in bc_lease.lease_tenants.filter(declined=False)
    )


def test_a_per_tenant_cleaning_deposit_leaves_the_lease_date_alone(
    landlord, bc_lease
):
    """One date can't describe three roommates paying separately, so an
    individual cleaning deposit stays on that tenant's paid flag."""
    from .billing import stamp_deposit_received

    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_lease.property,
        amount="75.00",
        due_date=date.today(),
        entry_type=EntryType.DEPOSIT_CHARGE,
        description="Cleaning deposit (individual)",
        metadata={"kind": "cleaning_deposit_individual"},
    )
    services.record_payment(
        charge=charge, amount="75.00", payment_method="ETRANSFER"
    )

    stamp_deposit_received(charge)
    bc_lease.refresh_from_db()
    assert bc_lease.cleaning_deposit_received_date is None


# --------------------------------------------------------- split payments
def _deposit_charge(landlord, lease, description, kind, amount="200.00"):
    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=lease,
        property=lease.property,
        amount=amount,
        due_date=date.today(),
        entry_type=EntryType.DEPOSIT_CHARGE,
        description=description,
        metadata={"kind": kind},
    )
    return charge


def test_one_transfer_settles_both_deposits_as_separate_payments(landlord, bc_lease):
    """Deposits arrive as one bank line but must stay two charges, because
    they are returned separately at the end of the tenancy."""
    security = _deposit_charge(
        landlord, bc_lease, "Security deposit", "security_deposit"
    )
    cleaning = _deposit_charge(
        landlord, bc_lease, "Cleaning deposit", "cleaning_deposit_lease"
    )

    posted = services.record_split_payment(
        landlord=landlord,
        allocations=[(security, Decimal("200.00")), (cleaning, Decimal("200.00"))],
        payment_method="ETRANSFER",
    )

    assert [created for _, created in posted] == [True, True]
    assert {entry.settles_id for entry, _ in posted} == {security.pk, cleaning.pk}
    assert all("Allocated from $400.00" in entry.description for entry, _ in posted)
    assert services.outstanding_on(security) == Decimal("0.00")
    assert services.outstanding_on(cleaning) == Decimal("0.00")
    assert services.deposits_held(landlord) == Decimal("400.00")


def test_resubmitting_a_split_does_not_double_record(landlord, bc_lease):
    security = _deposit_charge(
        landlord, bc_lease, "Security deposit", "security_deposit"
    )
    cleaning = _deposit_charge(
        landlord, bc_lease, "Cleaning deposit", "cleaning_deposit_lease"
    )
    allocations = [(security, Decimal("200.00")), (cleaning, Decimal("200.00"))]

    services.record_split_payment(
        landlord=landlord, allocations=allocations, payment_method="ETRANSFER"
    )
    again = services.record_split_payment(
        landlord=landlord, allocations=allocations, payment_method="ETRANSFER"
    )

    assert [created for _, created in again] == [False, False]
    assert services.deposits_held(landlord) == Decimal("400.00")


def test_a_split_cannot_reach_another_landlords_charge(landlord, bc_lease):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    other = LandlordProfile.objects.create(user=UserFactory())
    mine = _deposit_charge(landlord, bc_lease, "Security deposit", "security_deposit")
    with pytest.raises(services.LedgerError):
        services.record_split_payment(
            landlord=other,
            allocations=[(mine, Decimal("200.00"))],
            payment_method="ETRANSFER",
        )


def test_the_split_suggestion_finds_security_plus_cleaning(landlord, bc_lease):
    security = _deposit_charge(
        landlord, bc_lease, "Security deposit", "security_deposit"
    )
    cleaning = _deposit_charge(
        landlord, bc_lease, "Cleaning deposit", "cleaning_deposit_lease"
    )

    chosen = services.suggest_deposit_split([security, cleaning], Decimal("400.00"))
    assert {c.pk for c in chosen} == {security.pk, cleaning.pk}
    # An amount that matches nothing cleanly gets no guess at all.
    assert services.suggest_deposit_split([security, cleaning], Decimal("397.00")) is None


def test_the_split_endpoint_posts_one_payment_per_charge(landlord, bc_lease):
    security = _deposit_charge(
        landlord, bc_lease, "Security deposit", "security_deposit"
    )
    cleaning = _deposit_charge(
        landlord, bc_lease, "Cleaning deposit", "cleaning_deposit_lease"
    )
    client = APIClient()
    client.force_authenticate(user=landlord.user)

    suggested = client.get(
        f"/api/ledger/entries/suggest_split/?amount=400&lease={bc_lease.pk}"
    ).json()
    assert suggested["matched"] is True
    assert len(suggested["allocations"]) == 2

    response = client.post(
        "/api/ledger/entries/record_split_payment/",
        {
            "amount": "400.00",
            "payment_method": "ETRANSFER",
            "allocations": [
                {"charge_id": row["charge_id"], "amount": row["amount"]}
                for row in suggested["allocations"]
            ],
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    assert len(response.data["payments"]) == 2
    assert services.outstanding_on(security) == Decimal("0.00")
    assert services.outstanding_on(cleaning) == Decimal("0.00")


def test_the_split_endpoint_refuses_a_total_that_does_not_add_up(landlord, bc_lease):
    """The stated total is the landlord's own arithmetic against their bank
    line — silently accepting a mismatch is how a deposit ends up half-paid."""
    security = _deposit_charge(
        landlord, bc_lease, "Security deposit", "security_deposit"
    )
    client = APIClient()
    client.force_authenticate(user=landlord.user)

    response = client.post(
        "/api/ledger/entries/record_split_payment/",
        {
            "amount": "400.00",
            "payment_method": "ETRANSFER",
            "allocations": [{"charge_id": str(security.pk), "amount": "200.00"}],
        },
        format="json",
    )

    assert response.status_code == 400
    assert services.outstanding_on(security) == Decimal("200.00")


# ---------------------------------------------------------------------------
# August 2026: "$1,175 = the rest of her deposit + her August rent"
# ---------------------------------------------------------------------------


def _open_charges_for(lease):
    from rentium.ledger.models import CHARGE_TYPES, LedgerEntry

    return list(
        LedgerEntry.objects.with_settlement().filter(
            lease=lease, entry_type__in=CHARGE_TYPES, reversed_by__isnull=True
        )
    )


@pytest.mark.django_db
def test_one_transfer_can_cover_a_part_paid_deposit_and_the_rent(landlord, bc_lease):
    """A deposit charge in the pool used to hide rent from the split entirely.

    The landlord said in as many words what the $1,175 was; the suggestion still
    came back None because deposit-like charges REPLACED the pool rather than
    being preferred within it.
    """
    from datetime import date

    from rentium.ledger.models import EntryType, LedgerEntry
    from rentium.ledger.services import record_payment, suggest_deposit_split

    deposit = LedgerEntry.objects.create(
        landlord=landlord,
        lease=bc_lease,
        property=bc_lease.property,
        entry_type=EntryType.DEPOSIT_CHARGE,
        amount=Decimal("425.00"),
        due_date=date(2026, 7, 22),
        effective_date=date(2026, 7, 22),
        description="Security deposit — due on signing",
    )
    record_payment(charge=deposit, amount=Decimal("100.00"), payment_method="ETRANSFER")
    august = LedgerEntry.objects.create(
        landlord=landlord,
        lease=bc_lease,
        property=bc_lease.property,
        entry_type=EntryType.RENT_CHARGE,
        amount=Decimal("850.00"),
        due_date=date(2026, 8, 1),
        effective_date=date(2026, 8, 1),
        description="Monthly rent — period starting 2026-08-01",
    )

    split = suggest_deposit_split(_open_charges_for(bc_lease), Decimal("1175.00"))

    assert split is not None, "$325 owing + $850 rent is exactly $1,175"
    assert {c.pk for c in split} == {deposit.pk, august.pk}


@pytest.mark.django_db
def test_a_split_settles_the_oldest_charge_first(landlord, bc_lease):
    """Two identical rent charges are interchangeable to a subset sum.

    Without an ordering rule the August payment can land on September, leaving
    August open — the tenant shows as in arrears for a month they have paid.
    """
    from datetime import date

    from rentium.ledger.models import EntryType, LedgerEntry
    from rentium.ledger.services import suggest_deposit_split

    common = {
        "landlord": landlord,
        "lease": bc_lease,
        "property": bc_lease.property,
        "entry_type": EntryType.RENT_CHARGE,
        "amount": Decimal("850.00"),
    }
    august = LedgerEntry.objects.create(
        **common,
        due_date=date(2026, 8, 1),
        effective_date=date(2026, 8, 1),
        description="Monthly rent — August",
    )
    LedgerEntry.objects.create(
        **common,
        due_date=date(2026, 9, 1),
        effective_date=date(2026, 9, 1),
        description="Monthly rent — September",
    )
    fee = LedgerEntry.objects.create(
        landlord=landlord,
        lease=bc_lease,
        property=bc_lease.property,
        entry_type=EntryType.FEE_CHARGE,
        amount=Decimal("19.78"),
        due_date=date(2026, 7, 26),
        effective_date=date(2026, 7, 26),
        description="Damage recovery",
    )

    split = suggest_deposit_split(_open_charges_for(bc_lease), Decimal("869.78"))

    assert split is not None
    assert {c.pk for c in split} == {august.pk, fee.pk}, "September must not be paid first"


@pytest.mark.django_db
def test_an_inexact_amount_still_refuses_to_guess(landlord, bc_lease):
    from datetime import date

    from rentium.ledger.models import EntryType, LedgerEntry
    from rentium.ledger.services import suggest_deposit_split

    LedgerEntry.objects.create(
        landlord=landlord,
        lease=bc_lease,
        property=bc_lease.property,
        entry_type=EntryType.DEPOSIT_CHARGE,
        amount=Decimal("425.00"),
        due_date=date(2026, 7, 22),
        effective_date=date(2026, 7, 22),
        description="Security deposit",
    )
    LedgerEntry.objects.create(
        landlord=landlord,
        lease=bc_lease,
        property=bc_lease.property,
        entry_type=EntryType.RENT_CHARGE,
        amount=Decimal("850.00"),
        due_date=date(2026, 8, 1),
        effective_date=date(2026, 8, 1),
        description="Monthly rent",
    )

    # $1,200 is neither charge nor both — a split leaving a stray balance is
    # worse than asking.
    assert suggest_deposit_split(_open_charges_for(bc_lease), Decimal("1200.00")) is None
