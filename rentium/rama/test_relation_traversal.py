"""Cross-entity questions, and the safety boundary that makes them safe.

Asked "have I received deposits from everyone who was to move in from August?",
RAMA read the ledger once PER LEASE, guessed a date-range syntax, got a raw
Django ValidationError that taught it nothing, and burned its 45-second budget
without answering. Three separate gaps, one cause: the manifest was a flat list
of fields per entity, so nothing could be asked across two of them.

The manifest is now a graph. These tests pin what that opened up — and, more
carefully, what it did not.
"""

from __future__ import annotations

from datetime import date

import pytest

from rentium.ledger import services as ledger_services
from rentium.ledger.models import EntryType
from rentium.ledger.models import PaymentMethod
from rentium.rama.domain_read import read
from rentium.rama.manifest import MANIFEST
from rentium.rama.manifest import relations_for
from rentium.rama.manifest import resolve_path

pytestmark = pytest.mark.django_db

AUG = date(2026, 8, 1)


@pytest.fixture
def other_landlord(db):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


def _deposit(landlord, lease, amount, due=AUG):
    charge, _ = ledger_services.post_charge(
        landlord=landlord, tenant=None, lease=lease, property=lease.property,
        amount=amount, due_date=due, entry_type=EntryType.DEPOSIT_CHARGE,
        description="Security deposit — due on signing",
    )
    return charge


# --------------------------------------------------------------- the question

def test_deposits_for_august_movers_in_one_query(landlord, bc_lease):
    """The whole question, one call — not one call per lease."""
    bc_lease.start_date = date(2026, 8, 4)
    bc_lease.save(update_fields=["start_date"])
    charge = _deposit(landlord, bc_lease, "425.00")
    ledger_services.record_payment(
        charge=charge, amount="100.00",
        payment_method=PaymentMethod.ETRANSFER, payment_date=AUG,
    )

    result = read(
        landlord,
        entity="ledger_entry",
        filters="entry_type=DEPOSIT_CHARGE, lease__start_date=2026-08-01..2026-08-31",
        group_by="lease",
        aggregate="count, sum:amount, sum:settled_amount, sum:outstanding",
    )

    assert "error" not in result
    row = result["groups"][0]
    # Grouped by the lease's HUMAN label, not its UUID.
    assert row["lease"] == bc_lease.lease_number
    assert row["sum_amount"] == "425.00"
    assert row["sum_settled_amount"] == "100.00"
    assert row["sum_outstanding"] == "325.00"


def test_a_lease_starting_outside_august_is_excluded(landlord, bc_lease):
    """The filter is doing real work across the relation."""
    bc_lease.start_date = date(2026, 6, 1)
    bc_lease.save(update_fields=["start_date"])
    _deposit(landlord, bc_lease, "425.00")

    result = read(
        landlord, entity="ledger_entry",
        filters="lease__start_date=2026-08-01..2026-08-31",
        aggregate="count",
    )
    assert result["totals"]["count"] == 0


def test_the_range_syntax_the_model_guessed(landlord, bc_lease):
    """`field=a..b` on the field it cares about, not just the default date."""
    bc_lease.start_date = date(2026, 8, 15)
    bc_lease.save(update_fields=["start_date"])
    result = read(
        landlord, entity="lease", filters="start_date=2026-08-01..2026-08-31",
    )
    assert "error" not in result
    assert result["total_matched"] == 1


def test_lease_tenant_can_reach_its_lease(landlord, bc_lease):
    """The exact filter that failed: 'Can't filter on lease__lease_number'."""
    result = read(
        landlord, entity="lease_tenant",
        filters=f"lease__lease_number={bc_lease.lease_number}",
    )
    assert "error" not in result


# ------------------------------------------------------- the safety boundary

def test_only_manifest_entities_are_reachable(landlord):
    """The security boundary, and it maintains itself.

    `created_by` and `landlord` point at User/LandlordProfile, which nobody has
    decided what is safe to expose from. They must not be traversable, and they
    become so only when somebody deliberately makes them entities.
    """
    spec = MANIFEST["ledger_entry"]
    targets = relations_for(spec)
    assert "lease" in targets
    assert "created_by" not in targets
    assert "landlord" not in targets
    assert "tenant" not in targets

    result = read(landlord, entity="ledger_entry", filters="created_by__email=x@y.z")
    assert "Can't filter on" in result["error"]


def test_an_undeclared_field_stays_undeclared_across_a_relation(landlord):
    """Traversal grants no field any exposure it did not already have."""
    assert "house_rules" not in MANIFEST["lease"].field_map()
    result = read(landlord, entity="ledger_entry", filters="lease__house_rules~pets")
    assert "no field 'house_rules'" in result["error"]
    # And it teaches what IS available through that relation.
    assert "start_date" in result["error"]


def test_reverse_and_to_many_relations_are_not_traversable(landlord):
    """A to-many join fans the parent row out and multiplies every aggregate.

    The same defect was just removed from with_settlement() by making it a
    Subquery. Forbidding to-many traversal prevents it structurally instead of
    by remembering.
    """
    for spec in MANIFEST.values():
        from django.apps import apps

        model = apps.get_model(*spec.model.split("."))
        by_name = {f.name: f for f in model._meta.get_fields()}
        for name in relations_for(spec):
            field_ = by_name[name]
            assert field_.many_to_one or field_.one_to_one, (
                f"{spec.key}.{name} is to-many and must not be traversable"
            )


def test_traversal_cannot_widen_scope(landlord, other_landlord, bc_lease):
    """Scope is applied first and unconditionally; a join can only narrow."""
    bc_lease.start_date = date(2026, 8, 4)
    bc_lease.save(update_fields=["start_date"])
    _deposit(landlord, bc_lease, "425.00")

    theirs = read(
        other_landlord, entity="ledger_entry",
        filters="lease__start_date=2026-08-01..2026-08-31",
        aggregate="count, sum:amount",
    )
    assert theirs["totals"]["count"] == 0
    assert theirs["totals"]["sum_amount"] is None or theirs["totals"][
        "sum_amount"
    ] in ("0.00", "0")


def test_depth_is_bounded(landlord):
    spec = MANIFEST["ledger_entry"]
    # Depth 2 is fine: ledger_entry -> lease -> property.
    path, fieldspec, err = resolve_path(spec, "lease__property__name")
    assert err is None and path == "lease__property__name" and fieldspec is not None
    # Depth 3 is refused with a usable suggestion.
    _p, _f, err = resolve_path(spec, "lease__property__group__name")
    assert err and "deep" in err


def test_a_computed_field_is_not_reachable_through_a_relation(landlord):
    """`charge_state` is an annotation this query's queryset never produced."""
    result = read(landlord, entity="business_document",
                  filters="ledger_entry__charge_state=OVERDUE")
    assert "computed field" in result["error"]


#: The complete set of fields `update` may write, pinned. Read reach grew a lot
#: in this change; write reach must not have moved at all — scope filtering is
#: what makes a wider READ safe, and it says nothing about writes.
EDITABLE_SURFACE = {
    # inquiry
    ("inquiry", "landlord_notes"),
    # inventory
    ("inventory", "condition"),
    ("inventory", "description"),
    ("inventory", "location_description"),
    ("inventory", "name"),
    ("inventory", "quantity"),
    # lease
    ("lease", "cleaning_deposit"),
    ("lease", "end_date"),
    ("lease", "etransfer_email"),
    ("lease", "is_month_to_month"),
    ("lease", "landlord_daytime_phone"),
    ("lease", "landlord_fax"),
    ("lease", "landlord_other_phone"),
    ("lease", "landlord_service_address"),
    ("lease", "landlord_service_email"),
    ("lease", "move_in_date"),
    ("lease", "move_out_date"),
    ("lease", "parking_description"),
    ("lease", "parking_extra_charge"),
    ("lease", "parking_included"),
    ("lease", "pet_deposit"),
    ("lease", "pets_allowed"),
    ("lease", "pets_terms"),
    ("lease", "rent_due_day"),
    ("lease", "security_deposit"),
    ("lease", "services_and_facilities"),
    ("lease", "smoking_allowed"),
    ("lease", "smoking_terms"),
    ("lease", "special_terms"),
    ("lease", "start_date"),
    # lease_tenant
    ("lease_tenant", "cleaning_deposit"),
    ("lease_tenant", "rent_amount"),
    # property
    ("property", "address"),
    ("property", "asking_rent"),
    ("property", "available_from"),
    ("property", "city"),
    ("property", "description"),
    ("property", "is_publicly_visible"),
    ("property", "name"),
    ("property", "neighbourhood"),
    ("property", "postal_code"),
    ("property", "property_category"),
    ("property", "province"),
    ("property", "status"),
    # work_order
    ("work_order", "category"),
    ("work_order", "contractor_name"),
    ("work_order", "contractor_phone"),
    ("work_order", "cost"),
    ("work_order", "description"),
    ("work_order", "priority"),
    ("work_order", "scheduled_date"),
    ("work_order", "title"),
}


def test_the_write_surface_did_not_move():
    """Read reach grew; write reach must not have.

    Scope filtering makes wider reads safe. It does not make wider writes safe,
    and that asymmetry is the whole argument for deriving read access while
    keeping `editable` hand-declared and default-deny.
    """
    actual = {
        (key, name)
        for key, spec in MANIFEST.items()
        for name in spec.editable_map()
    }
    assert actual == EDITABLE_SURFACE


# --------------------------------------------------- no raw exception escapes

@pytest.mark.parametrize(
    "filters",
    [
        "start_date=not-a-date",
        "start_date=2026-13-45..2026-99-99",
        "start_date=..",
        "start_date=2026-08-01..",
        "lease__=5",
        "__=5",
        "lease__lease__lease__x=1",
        "total_rent=abc",
        "=",
        "~~~",
        "property__name",
    ],
)
def test_malformed_filters_return_a_sentence_not_a_traceback(landlord, filters):
    result = read(landlord, entity="lease", filters=filters)
    assert isinstance(result, dict)
    if "error" in result:
        assert isinstance(result["error"], str) and result["error"].strip()
        assert "Traceback" not in result["error"]


def test_a_validation_error_is_translated(landlord):
    """The exact class of error the model received verbatim and learned nothing from."""
    result = read(landlord, entity="lease", filters="start_date=nonsense")
    assert "error" in result
    assert "YYYY-MM-DD" in result["error"] or "date" in result["error"].lower()


# --------------------------------------------------------------- regressions

def test_the_previously_fixed_questions_still_work(landlord, bc_lease):
    rents = read(
        landlord, entity="ledger_entry", filters="entry_type=RENT_CHARGE",
        month="2026-08", group_by="charge_state",
        aggregate="count, sum:amount, sum:outstanding",
    )
    assert "error" not in rents
    arrears = read(
        landlord, entity="ledger_entry", filters="charge_state=OVERDUE",
        order_by="due_date", aggregate="count, sum:outstanding",
    )
    assert "error" not in arrears


def test_the_retired_aliases_still_resolve(landlord, bc_lease):
    """The 5 hardcoded relation filters are gone; the general path covers them."""
    for entity, clause in (
        ("lease", f"property_name~{bc_lease.property.name[:4]}"),
        ("work_order", "property_name~Maple"),
    ):
        result = read(landlord, entity=entity, filters=clause)
        assert "error" not in result, result


# ------------------------------------------------- discovery and reconciliation

def test_the_catalogue_is_an_index_until_you_ask_for_detail():
    """Thirteen entities' fields is ~3,300 tokens the turn mostly doesn't need."""
    from rentium.rama.manifest import entity_catalogue

    index = entity_catalogue()
    assert len(index) == len(MANIFEST)
    assert "fields" not in index[0], "the index must not carry every field"
    assert "relations" in index[0], "but it must show the graph"

    detail = entity_catalogue("lease")
    assert len(detail) == 1
    assert detail[0]["entity"] == "lease"
    assert any(f["name"] == "start_date" for f in detail[0]["fields"])

    unknown = entity_catalogue("nope")
    assert "error" in unknown[0] and "entities" in unknown[0]


def test_the_digest_advertises_traversal():
    """Traversal the model can't discover is traversal it won't use.

    Nothing told it ledger_entry and lease were connected, so it read the
    ledger once per lease.
    """
    from rentium.rama.manifest import capability_digest

    digest = capability_digest()
    assert "rel__field" in digest or "lease__start_date" in digest
    assert "ledger_entry → " in digest


def test_deposit_divergence_is_flagged_not_resolved_silently(landlord, bc_lease):
    """Two records of one deposit that disagree: say so rather than pick one."""
    from rentium.events.models import DomainEvent
    from rentium.rama.sergeants import check_deposit_record_divergence

    charge = _deposit(landlord, bc_lease, "425.00")
    ledger_services.record_payment(
        charge=charge, amount="100.00",
        payment_method=PaymentMethod.ETRANSFER, payment_date=AUG,
    )
    # Money in the ledger, nothing on the lease paperwork.
    assert bc_lease.security_deposit_received_date is None

    report = check_deposit_record_divergence()
    assert report["findings_published"] == 1

    finding = DomainEvent.objects.filter(
        event_type="rama.sentinel.deposit_record_divergence",
    ).first()
    assert finding.payload["lease_number"] == bc_lease.lease_number
    assert finding.payload["ledger_shows_money_received"] is True
    assert finding.payload["lease_marked_received"] is False
    assert finding.payload["ledger_settled_total"] == "100.00"


def test_agreement_is_not_flagged(landlord, bc_lease):
    """Both records saying the same thing is not a finding."""
    from rentium.rama.sergeants import check_deposit_record_divergence

    charge = _deposit(landlord, bc_lease, "425.00")
    ledger_services.record_payment(
        charge=charge, amount="425.00",
        payment_method=PaymentMethod.ETRANSFER, payment_date=AUG,
    )
    bc_lease.security_deposit_received_date = AUG
    bc_lease.save(update_fields=["security_deposit_received_date"])

    assert check_deposit_record_divergence()["findings_published"] == 0


def test_the_lease_deposit_dates_are_labelled_as_paperwork_not_money(landlord):
    """A field called '…received date' will be quoted as proof of payment."""
    fields = MANIFEST["lease"].field_map()
    for name in (
        "security_deposit_received_date",
        "cleaning_deposit_received_date",
        "pet_deposit_received_date",
    ):
        assert name in fields, f"{name} exists on the model and must be readable"
        assert "not the ledger" in fields[name].label
        assert not fields[name].editable, "the ledger is where money is recorded"
