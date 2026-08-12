"""`with_charge_state()` and `charge_status()` must never disagree.

Two definitions of the same fact are a liability: the per-object
`LedgerEntry.charge_status()` answers a detail view, and the SQL
`with_charge_state()` answers a GROUP BY. If they drift, RAMA reports one number
in a summary and a different one when you open the charge — and there is no
error, no exception, nothing to notice. So the SQL is not reviewed against the
Python, it is asserted against it, over a matrix that reaches every branch.
"""

from __future__ import annotations

from datetime import date
from datetime import timedelta
from decimal import Decimal

import pytest

from rentium.ledger import services as ledger_services
from rentium.ledger.models import CHARGE_TYPES
from rentium.ledger.models import ChargeStatus
from rentium.ledger.models import EntryType
from rentium.ledger.models import LedgerEntry
from rentium.ledger.models import PaymentMethod

pytestmark = pytest.mark.django_db

TODAY = date(2026, 8, 10)


def _charge(landlord, lease, amount, due, entry_type=EntryType.RENT_CHARGE):
    charge, _ = ledger_services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=lease,
        property=lease.property,
        amount=amount,
        due_date=due,
        entry_type=entry_type,
        description="matrix row",
    )
    return charge


@pytest.fixture
def matrix(landlord, bc_lease):
    """One charge per branch of charge_status(), plus a non-charge."""
    rows = {
        "scheduled": _charge(landlord, bc_lease, "850.00", TODAY + timedelta(days=20)),
        "due_today": _charge(landlord, bc_lease, "850.00", TODAY),
        "overdue": _charge(landlord, bc_lease, "850.00", TODAY - timedelta(days=9)),
        "paid": _charge(landlord, bc_lease, "800.00", TODAY - timedelta(days=30)),
        "partial": _charge(landlord, bc_lease, "900.00", TODAY - timedelta(days=2)),
        "overpaid": _charge(landlord, bc_lease, "100.00", TODAY - timedelta(days=3)),
        "voided": _charge(landlord, bc_lease, "8500.00", TODAY),
        "deposit": _charge(
            landlord, bc_lease, "425.00", TODAY, EntryType.DEPOSIT_CHARGE,
        ),
    }
    for key, paid in (("paid", "800.00"), ("partial", "300.00"),
                      ("overpaid", "150.00")):
        ledger_services.record_payment(
            charge=rows[key], amount=paid,
            payment_method=PaymentMethod.ETRANSFER, payment_date=TODAY,
        )
    ledger_services.void_entry(rows["voided"], reason="typo")
    return rows


def test_sql_and_python_agree_on_every_row(landlord, matrix):
    annotated = LedgerEntry.objects.filter(landlord=landlord).with_charge_state(
        today=TODAY,
    )
    seen = set()
    for entry in annotated:
        if entry.entry_type not in CHARGE_TYPES:
            assert entry.charge_state is None, (
                f"{entry.entry_type} is not a receivable and must have no state"
            )
            continue
        expected = entry.charge_status(today=TODAY)
        assert entry.charge_state == expected, (
            f"{entry.description} {entry.amount}: SQL said {entry.charge_state}, "
            f"Python said {expected}"
        )
        seen.add(expected)

    # The matrix is only worth anything if it actually reached every branch.
    assert seen == {
        ChargeStatus.SCHEDULED,
        ChargeStatus.DUE,
        ChargeStatus.OVERDUE,
        ChargeStatus.PAID,
        ChargeStatus.PARTIALLY_PAID,
        ChargeStatus.VOIDED,
    }, f"branches never exercised: {seen}"


def test_voided_outranks_paid(landlord, bc_lease):
    """Order matters: a reversed charge is not a collected one."""
    charge = _charge(landlord, bc_lease, "500.00", TODAY - timedelta(days=1))
    ledger_services.void_entry(charge, reason="raised in error")

    entry = (
        LedgerEntry.objects.filter(pk=charge.pk).with_charge_state(today=TODAY).get()
    )
    assert entry.charge_state == ChargeStatus.VOIDED
    assert entry.charge_state == entry.charge_status(today=TODAY)


def test_an_overpaid_charge_is_paid_not_partial(landlord, matrix):
    entry = (
        LedgerEntry.objects.filter(pk=matrix["overpaid"].pk)
        .with_charge_state(today=TODAY)
        .get()
    )
    assert entry.settled_amount == Decimal("150.00")
    assert entry.charge_state == ChargeStatus.PAID


def test_damage_claims_are_flagged(landlord, bc_lease):
    """A FEE_CHARGE with a work order is damage recovery; a late fee is income.

    Same entry type, opposite accounting treatment — and the only thing telling
    them apart is the work order. Both directions asserted, because the flag
    exists to keep damage recovery out of expected income.
    """
    from rentium.maintenance.models import WorkOrder

    late_fee = _charge(landlord, bc_lease, "50.00", TODAY, EntryType.FEE_CHARGE)
    work_order = WorkOrder.objects.create(
        property=bc_lease.property,
        title="Shower leak",
        category=WorkOrder.Category.PLUMBING,
    )
    damage, _ = ledger_services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_lease.property,
        amount="19.78",
        due_date=TODAY,
        entry_type=EntryType.FEE_CHARGE,
        description="damage recovery",
        work_order=work_order,
    )

    states = {
        entry.pk: entry.is_damage_claim
        for entry in LedgerEntry.objects.filter(
            pk__in=[late_fee.pk, damage.pk],
        ).with_charge_state(today=TODAY)
    }
    assert states[late_fee.pk] is False, "a late fee is ordinary income"
    assert states[damage.pk] is True, "damage recovery is not income"

    # And it agrees with the queryset the rest of the codebase uses.
    claimed = set(
        LedgerEntry.objects.filter(landlord=landlord)
        .damage_claims()
        .values_list("pk", flat=True),
    )
    assert claimed == {damage.pk}


def test_settlement_annotations_come_along(landlord, matrix):
    """with_charge_state includes with_settlement, so callers need only one."""
    entry = (
        LedgerEntry.objects.filter(pk=matrix["partial"].pk)
        .with_charge_state(today=TODAY)
        .get()
    )
    assert entry.settled_amount == Decimal("300.00")
    assert entry.outstanding == Decimal("600.00")
