"""
The ledger watches itself.

The July 2026 Financial page was wrong in four ways at once and every number in
the database was correct. Nothing in the system noticed; the landlord did. These
tests cover the Sergeant that closes that gap — the invariants below are the
defects that actually shipped, each expressed as a check that would have caught
it on the next nightly run instead of waiting for someone to read the page
carefully.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rentium.ledger import services
from rentium.rama import sergeants

pytestmark = pytest.mark.django_db


def _findings(kind):
    from rentium.events.models import DomainEvent

    return DomainEvent.objects.filter(event_type=f"rama.sentinel.{kind}")


def _holding(landlord, name="950 McKenzie Ave"):
    from rentium.properties.models import PropertyHolding

    return PropertyHolding.objects.create(
        landlord=landlord, name=name, address=name, city="Victoria"
    )


def _room(landlord, holding, name="Room C"):
    from rentium.properties.models import Property

    return Property.objects.create(
        landlord=landlord,
        holding=holding,
        name=name,
        address=holding.address,
        city="Victoria",
        province="BC",
        postal_code="V8V 1V1",
        property_category=Property.PropertyCategory.ROOM,
    )


def _expense(landlord, prop, amount="19.78", **kw):
    entry, _ = services.post_expense(
        landlord=landlord,
        amount=amount,
        category="MAINTENANCE",
        description="Hot water knob replacement",
        property=prop,
        **kw,
    )
    return entry


def _age_the_reversal(entry, days=5):
    """Voids are given a grace period so a same-session void→re-post doesn't
    trip the check. Backdate past it without mutating the original entry."""
    from rentium.ledger.models import LedgerEntry

    LedgerEntry.objects.filter(pk=entry.reversed_by.pk).update(
        effective_date=date.today() - timedelta(days=days)
    )


def test_an_abandoned_void_is_flagged(landlord):
    """Voided and never re-posted: money that vanished from the books with a
    reason nobody followed through on."""
    holding = _holding(landlord)
    entry = _expense(landlord, _room(landlord, holding))
    services.void_entry(entry, reason="Wrong room")
    entry.refresh_from_db()
    _age_the_reversal(entry)

    assert sergeants.check_ledger_integrity()["findings_published"] == 1
    finding = _findings("orphaned_void").first()
    assert finding.payload["amount"] == "19.78"
    assert finding.payload["reason"] == "Wrong room"


def test_a_completed_reallocation_is_not_flagged(landlord):
    """The correction that DID finish must stay quiet, or the check is noise."""
    holding = _holding(landlord)
    entry = _expense(landlord, _room(landlord, holding))
    services.reallocate_entry(
        entry, property=None, holding=holding, reason="Shared-space repair"
    )
    entry.refresh_from_db()
    _age_the_reversal(entry)

    assert sergeants.check_ledger_integrity()["findings_published"] == 0
    assert not _findings("orphaned_void").exists()


def test_a_fresh_void_is_given_time_to_be_re_posted(landlord):
    """A void and its replacement usually land seconds apart."""
    holding = _holding(landlord)
    entry = _expense(landlord, _room(landlord, holding))
    services.void_entry(entry, reason="About to re-post")

    assert sergeants.check_ledger_integrity()["findings_published"] == 0


def test_one_work_order_paid_for_twice_at_two_scopes_is_flagged(landlord):
    """The $19.78 shape: a mis-scoped cost 'fixed' by posting a second one
    somewhere else instead of reallocating the first."""
    from rentium.maintenance.models import WorkOrder

    holding = _holding(landlord)
    room = _room(landlord, holding)
    work_order = WorkOrder.objects.create(
        property=room,
        title="Shower leak + hot water knob replacement",
        category=WorkOrder.Category.PLUMBING,
    )
    _expense(landlord, room, work_order=work_order)
    services.post_expense(
        landlord=landlord,
        amount="19.78",
        category="MAINTENANCE",
        description="Work order: shower leak repair",
        holding=holding,
        work_order=work_order,
    )

    assert sergeants.check_ledger_integrity()["findings_published"] == 1
    payload = _findings("duplicate_work_order_expense").first().payload
    assert payload["count"] == 2
    assert set(payload["places"]) == {"Room C", "950 McKenzie Ave"}


def test_two_costs_on_one_job_at_the_same_place_are_fine(landlord):
    """Parts and labour are two expenses, not a double-post."""
    from rentium.maintenance.models import WorkOrder

    holding = _holding(landlord)
    room = _room(landlord, holding)
    work_order = WorkOrder.objects.create(
        property=room, title="Shower leak", category=WorkOrder.Category.PLUMBING
    )
    _expense(landlord, room, amount="19.78", work_order=work_order)
    _expense(landlord, room, amount="120.00", work_order=work_order)

    assert sergeants.check_ledger_integrity()["findings_published"] == 0


def test_findings_are_idempotent(landlord):
    """Beat re-runs must not re-notify — same dedupe contract as every other
    Sergeant."""
    holding = _holding(landlord)
    entry = _expense(landlord, _room(landlord, holding))
    services.void_entry(entry, reason="Wrong room")
    entry.refresh_from_db()
    _age_the_reversal(entry)

    assert sergeants.check_ledger_integrity()["findings_published"] == 1
    assert sergeants.check_ledger_integrity()["findings_published"] == 0
    assert _findings("orphaned_void").count() == 1


def test_the_annotation_contract_holds(landlord):
    """If outstanding ever leaks back onto a non-charge, this reports it in
    production instead of waiting for someone to notice '$31.45 left' under a
    row marked Paid."""
    holding = _holding(landlord)
    _expense(landlord, _room(landlord, holding))

    sergeants.check_ledger_integrity()
    assert not _findings("ledger_contract_violation").exists()


def test_it_runs_as_part_of_the_nightly_sweep(landlord):
    report = sergeants.run_all()
    assert "ledger_integrity" in report
    assert "error" not in report["ledger_integrity"]
