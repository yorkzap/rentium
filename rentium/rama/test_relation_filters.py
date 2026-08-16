"""Filtering on a relation itself, not only on a field of it.

From the live run that answered "don't aishwarya and naveen received a discount
and only paid $400 for august?" correctly, but in one round more than it needed:

    read(entity='rent_adjustment', filters='lease=1b3c…')
      → "Can't filter on 'lease'. Filterable fields: adjustment_type, …"
    read(entity='rent_adjustment', filters='lease_tenant__lease__lease_number=…')
      → the answer

Nothing was ambiguous about the first call. A relation has exactly one primary
key and one human name, and `link` already searches those same columns when the
landlord names a thing. A round is a third of the turn's budget, so a syntax the
model reaches for and the manifest can resolve should not cost one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from rentium.rama.domain_read import read
from rentium.rama.manifest import MANIFEST
from rentium.rama.manifest import resolve_relation

pytestmark = pytest.mark.django_db

AUG = date(2026, 8, 1)


@pytest.fixture
def other_landlord(db):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


@pytest.fixture
def tenancy(bc_lease):
    """One lease-tenant on the lease, with a name to search for."""
    from rentium.leases.models import LeaseTenant

    lt = LeaseTenant.objects.filter(lease=bc_lease).first()
    if lt is None:
        lt = LeaseTenant.objects.create(
            lease=bc_lease,
            invited_name="Naveen Prasanth",
            invited_email="naveen@example.com",
            rent_amount=Decimal("2000"),
        )
    else:
        lt.invited_name = "Naveen Prasanth"
        lt.save(update_fields=["invited_name"])
    return lt


@pytest.fixture
def discount(landlord, tenancy):
    from rentium.leases.models import RentAdjustment

    return RentAdjustment.objects.create(
        lease_tenant=tenancy,
        created_by=landlord,
        adjustment_type=RentAdjustment.AdjustmentType.DISCOUNT,
        calculation_method=RentAdjustment.CalculationMethod.FLAT_AMOUNT,
        amount=Decimal("1600.00"),
        target_amount=Decimal("400.00"),
        reason="First-month household rent adjusted to $400 per landlord request.",
        effective_date=AUG,
        end_date=date(2026, 8, 31),
    )


# --------------------------------------------------------- the call that failed

def test_a_relation_can_be_filtered_by_id(landlord, tenancy, discount):
    """`lease_tenant=<uuid>` — the shape the model wrote, on the entity it wrote it."""
    result = read(
        landlord, entity="rent_adjustment", filters=f"lease_tenant={tenancy.pk}",
    )
    assert "error" not in result, result
    assert result["total_matched"] == 1


def test_a_relation_path_can_be_filtered_by_id(landlord, bc_lease, discount):
    """RentAdjustment has no lease FK; the lease is one hop further out.

    This is the call the model actually wanted, and `lease=<uuid>` on this
    entity is genuinely wrong — the manifest says so by not having that
    relation. `lease_tenant__lease=<uuid>` is right and must work.
    """
    result = read(
        landlord, entity="rent_adjustment",
        filters=f"lease_tenant__lease={bc_lease.pk}",
    )
    assert "error" not in result, result
    assert result["total_matched"] == 1


def test_a_relation_can_be_filtered_by_name(landlord, bc_lease, discount):
    """"the adjustments on RMT…-C281" — the lease number, not its uuid."""
    result = read(
        landlord, entity="rent_adjustment",
        filters=f"lease_tenant__lease={bc_lease.lease_number}",
    )
    assert "error" not in result, result
    assert result["total_matched"] == 1


def test_a_person_can_be_named(landlord, discount):
    """The landlord says "Naveen", not a uuid, and so does the model."""
    result = read(
        landlord, entity="rent_adjustment", filters="lease_tenant=Naveen",
    )
    assert "error" not in result, result
    assert result["total_matched"] == 1


def test_a_name_that_matches_nothing_is_simply_empty(landlord, discount):
    result = read(
        landlord, entity="rent_adjustment", filters="lease_tenant=Nobody",
    )
    assert "error" not in result, result
    assert result["total_matched"] == 0


def test_negation_works(landlord, discount):
    assert read(
        landlord, entity="rent_adjustment", filters="lease_tenant!=Naveen",
    )["total_matched"] == 0
    assert read(
        landlord, entity="rent_adjustment", filters="lease_tenant!=Nobody",
    )["total_matched"] == 1


def test_it_combines_with_ordinary_clauses(landlord, discount):
    """An OR inside one clause must still AND with the rest, not widen it."""
    hit = read(
        landlord, entity="rent_adjustment",
        filters="lease_tenant=Naveen, adjustment_type=DISCOUNT",
    )
    assert hit["total_matched"] == 1
    miss = read(
        landlord, entity="rent_adjustment",
        filters="lease_tenant=Naveen, adjustment_type=INCREASE",
    )
    assert miss["total_matched"] == 0


def test_grouping_by_a_relation_path(landlord, bc_lease, discount):
    """"discounts per lease" on an entity two hops from the lease."""
    result = read(
        landlord, entity="rent_adjustment", group_by="lease_tenant__lease",
        aggregate="count, sum:amount",
    )
    assert "error" not in result, result
    row = result["groups"][0]
    assert row["lease_tenant__lease"] == bc_lease.lease_number
    assert row["sum_amount"] == "1600.00"


# ------------------------------------------------------------ what it must not do

def test_naming_another_landlords_row_returns_nothing(
    landlord, other_landlord, tenancy, discount,
):
    """Scope is applied first; an id from outside it matches nothing.

    Not "is refused" — refusal would confirm the row exists. It simply isn't
    in the queryset being filtered.
    """
    result = read(
        other_landlord, entity="rent_adjustment",
        filters=f"lease_tenant={tenancy.pk}", aggregate="count",
    )
    assert result["totals"]["count"] == 0


def test_a_relation_to_a_non_entity_is_still_unreachable(landlord):
    """The security boundary is unchanged: only manifest entities resolve."""
    assert resolve_relation(MANIFEST["ledger_entry"], "created_by") is None
    result = read(landlord, entity="ledger_entry", filters="created_by=someone")
    assert "Can't filter on" in result["error"]


def test_comparison_operators_are_refused_with_a_usable_message(landlord):
    """`lease>x` means nothing. Say what does work rather than guessing."""
    result = read(landlord, entity="ledger_entry", filters="lease>2026-01-01")
    assert "error" in result
    assert "relation" in result["error"]
    assert "lease__<field>" in result["error"]


def test_depth_is_bounded_here_too(landlord):
    """Three relation hops is a different query, not a longer filter."""
    assert resolve_relation(MANIFEST["rent_adjustment"], "lease_tenant__lease") is not None
    assert resolve_relation(
        MANIFEST["rent_adjustment"], "lease_tenant__lease__property",
    ) is None


def test_a_declared_field_of_the_same_name_still_wins():
    """A name that is both a column and a relation resolves as the column.

    `_parse_filters` consults the field map before the relation graph, so the
    manifest stays the authority on what a word means. Where the two collide
    today the field is the one the landlord means — `property_group.property`
    is a count, not a link to follow.
    """
    from rentium.rama.domain_read import _parse_filters
    from rentium.rama.manifest import relations_for

    for key, spec in MANIFEST.items():
        for name in set(spec.field_map()) & set(relations_for(spec)):
            if not spec.field_map()[name].filterable:
                continue
            _inc, _exc, conds, _err = _parse_filters(
                f"{name}=1", spec.field_map(), spec,
            )
            assert not conds, (
                f"{key}.{name} is a declared field and must not be read as a "
                f"relation"
            )


# ------------------------------------------------- returning a related column

def test_a_related_field_can_be_returned(landlord, bc_lease, discount):
    """From the live run, six times in a row and never honoured.

    Asked which lease a discount belonged to, the model requested
    `fields='lease_tenant__lease, lease_tenant__tenant_email'`, got rows with
    neither key and no explanation, and re-asked until the budget ran out —
    then told the landlord it could not tell which lease it was.
    """
    result = read(
        landlord, entity="rent_adjustment",
        fields="amount, lease_tenant__lease, lease_tenant__lease__start_date",
    )
    assert "error" not in result, result
    row = result["rows"][0]
    assert row["amount"] == "1600.00"
    # A relation on its own returns the human name, not a uuid.
    assert row["lease_tenant__lease"] == bc_lease.lease_number
    assert row["lease_tenant__lease__start_date"] == bc_lease.start_date.isoformat()


def test_a_display_method_is_called_on_the_object_that_owns_it(landlord, discount):
    """`get_status_display` belongs to the lease, not to the adjustment."""
    result = read(
        landlord, entity="rent_adjustment",
        fields="adjustment_type, lease_tenant__lease__status",
    )
    row = result["rows"][0]
    assert row["adjustment_type"] == "Negotiated Discount"
    assert row["lease_tenant__lease__status"] == "Active"


def test_id_is_always_returnable(landlord, discount):
    result = read(landlord, entity="rent_adjustment", fields="id, amount")
    assert result["rows"][0]["id"] == str(discount.pk)


def test_an_unknown_field_is_named_and_the_rest_still_returned(landlord, discount):
    """Neither silence nor an all-or-nothing refusal.

    Silence cost a turn (the model re-asked six times). Failing the whole call
    for one bad name in a list of ten cost the same turn a different way — the
    model re-sends nearly the same list. Give it the rows and tell it which
    name it cannot have.
    """
    result = read(landlord, entity="rent_adjustment", fields="amount, nonsense")
    assert "error" not in result, result
    assert result["rows"][0]["amount"] == "1600.00"
    assert "nonsense" in result["fields_unavailable"]
    assert "nonsense" not in result["fields"]


def test_a_list_of_nothing_but_bad_names_is_an_error(landlord, discount):
    """There is no partial answer to give, so say so rather than return
    every field as if that was what was asked for."""
    result = read(landlord, entity="rent_adjustment", fields="nonsense, drivel")
    assert "error" in result
    assert "nonsense" in result["error"]
    assert "drivel" in result["error"]


def test_the_catalogue_is_not_repeated_per_rejected_name(landlord, discount):
    """Five bad names in a list of twenty must not return the field list five
    times — the reply is the model's next context window."""
    result = read(
        landlord, entity="rent_adjustment",
        fields="amount, no1, no2, no3, no4, no5",
    )
    assert result["fields_available"].count("effective_date") == 1
    for name in ("no1", "no2", "no3", "no4", "no5"):
        assert "effective_date" not in result["fields_unavailable"][name]


def test_an_undeclared_related_field_stays_undeclared(landlord, discount):
    """Projection grants no field exposure it did not already have."""
    result = read(
        landlord, entity="rent_adjustment",
        fields="amount, lease_tenant__lease__house_rules",
    )
    rejected = result["fields_unavailable"]
    assert "lease_tenant__lease__house_rules" in rejected
    # And it names what IS declared over there, so the next call is right.
    assert "start_date" in rejected["lease_tenant__lease__house_rules"]
    assert "house_rules" not in str(result["rows"])


def test_a_relation_to_a_non_entity_cannot_be_projected(landlord, discount):
    result = read(
        landlord, entity="rent_adjustment", fields="amount, created_by__email",
    )
    assert "created_by__email" in result["fields_unavailable"]


def test_a_null_relation_renders_as_none_not_a_crash(landlord, bc_lease):
    """An optional FK on the way to the column, unset."""
    result = read(landlord, entity="lease", fields="lease_number, property__name")
    assert "error" not in result, result
    assert result["rows"]


def test_the_group_by_error_names_paths_it_can_actually_reach(landlord, discount):
    """`group_by='lease'` on rent_adjustment is wrong, and the old message
    listed only immediate neighbours — none of which was the word 'lease'."""
    result = read(landlord, entity="rent_adjustment", group_by="lease")
    assert "error" in result
    assert "lease_tenant__lease" in result["error"]


def test_fields_with_group_by_says_it_was_ignored(landlord, discount):
    """Pinned from a live run: four identical calls, one after another.

    Asked for adjustments grouped by lease WITH the row detail, `read` returned
    groups and silently dropped `fields`. The model saw no rows, assumed the
    call had not landed, and re-sent it — the same silence-costs-a-turn defect
    as dropping an unknown field name.
    """
    result = read(
        landlord, entity="rent_adjustment", group_by="lease_tenant__lease",
        aggregate="count", fields="amount, reason",
    )
    assert "groups" in result
    assert "fields_ignored" in result
    assert "no group_by" in result["fields_ignored"].casefold()


def test_a_plain_grouped_call_says_nothing_extra(landlord, discount):
    """The note appears only when something was actually ignored."""
    result = read(
        landlord, entity="rent_adjustment", group_by="lease_tenant__lease",
        aggregate="count",
    )
    assert "fields_ignored" not in result


def test_order_by_accepts_the_aggregate_as_spelled(landlord, discount):
    """`aggregate='sum:amount', order_by='-sum:amount'` — one spelling, twice.

    Refusing the second cost a round on a query that was otherwise exactly
    right.
    """
    result = read(
        landlord, entity="rent_adjustment", group_by="lease_tenant__lease",
        aggregate="count, sum:amount", order_by="-sum:amount",
    )
    assert "error" not in result, result
    assert result["groups"]


def test_order_by_a_month_grouping(landlord, discount):
    result = read(
        landlord, entity="rent_adjustment", group_by="month:effective_date",
        aggregate="count", order_by="-month:effective_date",
    )
    assert "error" not in result, result


def test_no_traceback_escapes(landlord):
    for filters in ("lease=", "lease_tenant__lease=", "lease=..", "lease~"):
        result = read(landlord, entity="rent_adjustment", filters=filters)
        assert isinstance(result, dict)
        if "error" in result:
            assert "Traceback" not in result["error"]
