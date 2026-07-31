"""
No two readers of the ledger may disagree about the same money.

THE INCIDENT THIS ENCODES
-------------------------
A landlord asked whether the $100 Room C deposit payment was in the ledger.
RAMA checked and said no — "the charge is $425 with $325 outstanding". Pushed,
it checked again and said yes — "$100 paid, $325 outstanding". Same question,
same second, two answers, and the $425/$325 pair was identical in both.

Nothing was stale. `tenant_statement` returned a payload that contradicted
ITSELF: `paid_to_date: "100.00"` sitting next to `deposit_held: "0.00"`,
because the deposit aggregate filtered `tenant=tenant` while a joint-lease
deposit charge — and therefore the payment settling it — carries `tenant=None`.

The fix is `ledger/position.py`. This file is the guardrail that keeps it
fixed: it does not test one function, it asserts that every reader of a given
quantity returns the SAME Decimal. A future reader that hand-rolls its own
aggregate fails here rather than in a conversation.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from rentium.ledger import services as ledger_services
from rentium.ledger.models import EntryType
from rentium.ledger.position import Scope
from rentium.ledger.position import financial_position

pytestmark = pytest.mark.django_db

TODAY = datetime.date.today()
YESTERDAY = TODAY - datetime.timedelta(days=1)


# --------------------------------------------------------------- fixtures
@pytest.fixture
def roommate(db):
    from rentium.users.models import TenantProfile
    from rentium.users.tests.factories import UserFactory

    return TenantProfile.objects.create(user=UserFactory())


@pytest.fixture
def joint_lease(landlord, bc_lease, tenant, roommate):
    """A roommate lease: two tenants, household charges owed by both in full."""
    from rentium.leases.models import LeaseTenant

    for person in (tenant, roommate):
        # Their individual share of the rent. The DEPOSIT is deliberately not
        # split — it is a household charge both are liable for in full, which
        # is exactly the case the old tenant-scoped aggregate got wrong.
        LeaseTenant.objects.create(
            lease=bc_lease, tenant=person, rent_amount=Decimal("425.00")
        )
    return bc_lease


@pytest.fixture
def joint_deposit(landlord, joint_lease):
    """The $425 deposit charge — tenant=None, because the household owes it."""
    charge, _ = ledger_services.post_charge(
        landlord=landlord,
        property=joint_lease.property,
        lease=joint_lease,
        tenant=None,
        entry_type=EntryType.DEPOSIT_CHARGE,
        amount=Decimal("425.00"),
        due_date=YESTERDAY,
        description="Security deposit — due on signing",
    )
    return charge


@pytest.fixture
def hundred_paid(landlord, joint_deposit):
    """$100 of the $425, received by e-transfer. The money in the incident."""
    entry, _ = ledger_services.record_payment(
        charge=joint_deposit,
        amount=Decimal("100.00"),
        payment_method="ETRANSFER",
        payment_date=TODAY,
        idempotency_key="test-the-hundred",
    )
    return entry


# ------------------------------------------------- the incident, exactly
def test_every_reader_agrees_the_hundred_is_held(landlord, joint_lease, tenant, hundred_paid):
    """The bug: three readers of "deposit held", one of them answering 0.00.

    portfolio and lease scope said 100.00; tenant scope said 0.00 because it
    filtered on a column a joint-lease payment does not carry.
    """
    portfolio = ledger_services.deposits_held(landlord)
    by_lease = Decimal(
        ledger_services.deposit_position(landlord, lease=joint_lease)["deposit_held"]
    )
    by_tenant = Decimal(
        ledger_services.tenant_statement(landlord, tenant=tenant)["deposit_held"]
    )

    assert portfolio == by_lease == by_tenant == Decimal("100.00"), (
        f"readers disagree: portfolio={portfolio} lease={by_lease} tenant={by_tenant}"
    )


def _deposit_row(statement) -> dict:
    """The deposit line out of a tenant statement's charges."""
    rows = [c for c in statement["charges"] if "deposit" in c["description"].lower()]
    assert len(rows) == 1, f"expected one deposit charge, got {rows}"
    return rows[0]


def test_the_statement_does_not_contradict_itself(landlord, tenant, hundred_paid):
    """`paid_to_date: 100.00` next to `deposit_held: 0.00` in one payload is
    what let RAMA answer no and yes to the same question and be "right" both
    times. A payload that disagrees with itself is the actual defect.

    The deposit LINE and the deposit TOTAL have to tell the same story: $425
    charged, $100 in hand, $325 still to come.
    """
    statement = ledger_services.tenant_statement(landlord, tenant=tenant)
    row = _deposit_row(statement)

    assert Decimal(row["amount"]) == Decimal("425.00")
    assert Decimal(row["outstanding"]) == Decimal("325.00")
    assert row["is_joint"] is True
    assert Decimal(statement["deposit_held"]) == Decimal("100.00")
    # The line and the total agree: charged − held == outstanding.
    assert (
        Decimal(row["amount"]) - Decimal(statement["deposit_held"])
        == Decimal(row["outstanding"])
    )


def test_a_roommate_is_liable_for_the_whole_household_deposit(
    landlord, roommate, hundred_paid
):
    """Joint means each tenant owes the whole charge, not a share — so the
    second roommate's statement must show the same numbers, not zero."""
    statement = ledger_services.tenant_statement(landlord, tenant=roommate)
    row = _deposit_row(statement)

    assert Decimal(statement["deposit_held"]) == Decimal("100.00")
    assert Decimal(row["outstanding"]) == Decimal("325.00")


# ------------------------------------------------------- the agreement matrix
def _deposit_held_by_every_reader(landlord, *, lease, tenant) -> dict[str, Decimal]:
    """Every path in the codebase that answers "how much deposit is held"."""
    return {
        "services.deposits_held": ledger_services.deposits_held(landlord),
        "services.deposit_position": Decimal(
            ledger_services.deposit_position(landlord, lease=lease)["deposit_held"]
        ),
        "services.tenant_statement": Decimal(
            ledger_services.tenant_statement(landlord, tenant=tenant)["deposit_held"]
        ),
        "position.portfolio": financial_position(
            landlord, scope=Scope.portfolio()
        ).deposits_held,
        "position.lease": financial_position(
            landlord, scope=Scope.of_lease(lease)
        ).deposits_held,
        "position.tenant": financial_position(
            landlord, scope=Scope.of_tenant(tenant)
        ).deposits_held,
    }


@pytest.mark.parametrize(
    ("paid", "expected"),
    [
        (None, "0.00"),
        ("100.00", "100.00"),
        ("425.00", "425.00"),
    ],
)
def test_deposit_held_agrees_across_every_reader(
    landlord, joint_lease, joint_deposit, tenant, paid, expected
):
    """One landlord, one lease, one tenant — so every scope covers the same
    money and every reader must return the same number. Parametrised over
    unpaid / partly paid / fully paid because the old bug was invisible at
    0.00 and at nothing-recorded."""
    if paid is not None:
        ledger_services.record_payment(
            charge=joint_deposit,
            amount=Decimal(paid),
            payment_method="ETRANSFER",
            payment_date=TODAY,
            idempotency_key=f"agreement-{paid}",
        )

    readers = _deposit_held_by_every_reader(landlord, lease=joint_lease, tenant=tenant)

    assert set(readers.values()) == {Decimal(expected)}, (
        f"readers disagree: {', '.join(f'{k}={v}' for k, v in readers.items())}"
    )


def test_a_returned_deposit_reduces_every_reader_together(
    landlord, joint_lease, tenant, hundred_paid
):
    """A deposit return is a separate entry type, not a settlement, so it is
    the one place the scope predicate cannot go through `settles`. If a reader
    forgets it, this catches the drift."""
    ledger_services.post_deposit_return(
        landlord=landlord,
        lease=joint_lease,
        tenant=tenant,
        property=joint_lease.property,
        amount=Decimal("40.00"),
        description="Partial deposit return",
        payment_method="ETRANSFER",
        idempotency_key="agreement-return",
    )

    readers = _deposit_held_by_every_reader(landlord, lease=joint_lease, tenant=tenant)

    assert set(readers.values()) == {Decimal("60.00")}, (
        f"readers disagree after a return: "
        f"{', '.join(f'{k}={v}' for k, v in readers.items())}"
    )


# ------------------------------------------------- deposits are not income
def test_a_deposit_is_owed_but_never_counted_as_income(landlord, joint_deposit):
    """The distinction three different keys named `outstanding` were blurring:
    a deposit is genuinely OWED, and is never INCOME.

    Asserted as an identity rather than against fixed totals, because an active
    lease also posts rent — and the invariant has to hold whatever else is on
    the books, which is the whole point.
    """
    position = financial_position(landlord, scope=Scope.portfolio())

    assert position.deposits_outstanding == Decimal("425.00")
    # Income excludes deposits — this is why the dashboard could read $0.00
    # outstanding while a $425 deposit sat overdue in the ledger.
    assert (
        position.outstanding_now - position.income_outstanding_now
        == position.deposits_outstanding
    )


def test_a_future_charge_is_on_the_books_but_not_owed_yet(landlord, joint_lease):
    """`outstanding_now` vs `outstanding_all` — the two meanings that were both
    called `outstanding` in different tools."""
    before = financial_position(landlord, scope=Scope.of_lease(joint_lease))
    ledger_services.post_charge(
        landlord=landlord,
        property=joint_lease.property,
        lease=joint_lease,
        tenant=None,
        entry_type=EntryType.RENT_CHARGE,
        amount=Decimal("850.00"),
        due_date=TODAY + datetime.timedelta(days=365),
        description="Rent — far future",
    )
    after = financial_position(landlord, scope=Scope.of_lease(joint_lease))

    # On the books, but not owed yet. Two meanings, two names.
    assert after.outstanding_all - before.outstanding_all == Decimal("850.00")
    assert after.outstanding_now == before.outstanding_now
