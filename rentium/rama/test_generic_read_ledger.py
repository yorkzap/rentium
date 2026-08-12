"""The generic `read` must obey the same ledger rules as every other reader.

`read` applied only `{scope_path: landlord}`. Every other ledger reader in the
codebase — union.month_money, domain_reads.charge_schedule — starts from
`.not_voided()`, because a reversed charge is a correction, not money owed.
So "read every rent charge for August" counted charges that had been voided,
and the answer looked authoritative because it came from the books.

That was survivable while `read` returned rows a human skimmed. It stops being
survivable the moment it can sum them.
"""

from __future__ import annotations

from datetime import date

import pytest

from rentium.ledger import services as ledger_services
from rentium.ledger.models import EntryType
from rentium.rama.domain_read import read

pytestmark = pytest.mark.django_db


def _charge(landlord, lease, amount, due=None):
    charge, _ = ledger_services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=lease,
        property=lease.property,
        amount=amount,
        due_date=due or date(2026, 8, 1),
        entry_type=EntryType.RENT_CHARGE,
        description="Monthly rent",
    )
    return charge


@pytest.fixture
def other_landlord(db):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


def test_a_voided_charge_is_not_read_back_as_live(landlord, bc_lease):
    """The reversed charge disappears from the charges; the REVERSAL remains.

    That asymmetry is the point of an append-only ledger: the correction is
    itself a permanent entry. What must never happen is the voided charge still
    counting as money owed, which is what `read` was reporting.
    """
    _charge(landlord, bc_lease, "850.00")
    mistake = _charge(landlord, bc_lease, "8500.00")
    assert read(landlord, entity="ledger_entry", limit="50")["total_matched"] == 2

    ledger_services.void_entry(mistake, reason="typo — wrong amount")

    charges = read(
        landlord,
        entity="ledger_entry",
        filters="entry_type=RENT_CHARGE",
        limit="50",
    )
    assert charges["total_matched"] == 1, "the voided charge is no longer owed"
    assert charges["rows"][0]["amount"] == "850.00"

    # The REVERSAL is a real, permanent entry and is still readable as one.
    reversals = read(
        landlord, entity="ledger_entry", filters="entry_type=REVERSAL", limit="50",
    )
    assert reversals["total_matched"] == 1


def test_the_reply_says_what_was_excluded(landlord, bc_lease):
    """A total the model cannot explain is a total it will misreport."""
    _charge(landlord, bc_lease, "850.00")
    result = read(landlord, entity="ledger_entry", limit="50")
    assert result["scope_note"] == "voided/reversed entries excluded"


def test_scope_still_wins_over_every_filter(landlord, other_landlord, bc_lease):
    """base_queryset narrows; it must never be a way to widen scope."""
    _charge(landlord, bc_lease, "850.00")
    assert read(other_landlord, entity="ledger_entry", limit="50")["total_matched"] == 0


def test_a_typo_in_base_queryset_fails_loudly():
    from rentium.rama.manifest import EntitySpec

    spec = EntitySpec(
        key="x", model="ledger.LedgerEntry", label="x", scope_path="landlord",
        base_queryset=("not_voidedd",),
    )
    from rentium.ledger.models import LedgerEntry

    with pytest.raises(AttributeError, match="not_voidedd"):
        spec.base_queryset_for(LedgerEntry.objects)


def test_entities_without_base_queryset_are_unchanged(landlord):
    """The default is a no-op — this must not quietly filter other entities."""
    result = read(landlord, entity="property", limit="50")
    assert "error" not in result
    assert "scope_note" not in result
