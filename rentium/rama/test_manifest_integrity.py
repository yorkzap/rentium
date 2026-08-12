"""The manifest is data the model reasons over, so its rules are enforced, not reviewed.

Every field RAMA can filter, aggregate, group or order by is declared here. A
declaration that is subtly wrong — a `source` naming an annotation nothing
produces, an `aggregatable` string field, a `groupable` free-text field — does
not raise. It produces an answer. That is the failure mode this whole module is
built to avoid, so the invariants are asserted the same way the rest of the
manifest's guarantees are: mechanically, over every entity.
"""

from __future__ import annotations

import pytest
from django.apps import apps

from rentium.rama.manifest import MANIFEST

QUANTITY_TYPES = {"number", "money"}
BOUNDED_TYPES = {"enum", "bool"}

ALL_FIELDS = [
    pytest.param(spec, fieldspec, id=f"{spec.key}.{fieldspec.name}")
    for spec in MANIFEST.values()
    for fieldspec in spec.fields
]
ALL_ENTITIES = [pytest.param(spec, id=spec.key) for spec in MANIFEST.values()]


@pytest.mark.parametrize(("spec", "fieldspec"), ALL_FIELDS)
def test_only_quantities_are_aggregatable(spec, fieldspec):
    """sum/avg over a string is a number nobody can interpret."""
    if fieldspec.aggregatable:
        assert fieldspec.type in QUANTITY_TYPES, (
            f"{spec.key}.{fieldspec.name} is {fieldspec.type!r}; "
            f"aggregatable needs one of {sorted(QUANTITY_TYPES)}"
        )


@pytest.mark.parametrize(("spec", "fieldspec"), ALL_FIELDS)
def test_only_bounded_fields_are_groupable(spec, fieldspec):
    """Grouping by free text yields one group per row, which reads as a table."""
    if fieldspec.groupable:
        assert fieldspec.type in BOUNDED_TYPES, (
            f"{spec.key}.{fieldspec.name} is {fieldspec.type!r}; "
            f"groupable needs one of {sorted(BOUNDED_TYPES)}"
        )


@pytest.mark.parametrize(("spec", "fieldspec"), ALL_FIELDS)
def test_a_derived_field_names_itself(spec, fieldspec):
    """`source` is the annotation's name; anything else silently misreads."""
    if fieldspec.source:
        assert fieldspec.source == fieldspec.name, (
            f"{spec.key}.{fieldspec.name} declares source={fieldspec.source!r}"
        )
        assert spec.annotate, (
            f"{spec.key}.{fieldspec.name} is derived but {spec.key} declares "
            f"no annotate methods to produce it"
        )


@pytest.mark.parametrize("spec", ALL_ENTITIES)
def test_declared_queryset_methods_exist(spec):
    """Catches a typo at test time instead of at answer time."""
    model = apps.get_model(*spec.model.split("."))
    for name in (*spec.base_queryset, *spec.annotate):
        assert callable(getattr(model.objects, name, None)), (
            f"{spec.key} names queryset method {name!r}, which "
            f"{model.__name__}.objects does not have"
        )


@pytest.mark.parametrize("spec", ALL_ENTITIES)
def test_the_date_field_is_declared_and_a_date(spec):
    if spec.date_field:
        fieldspec = spec.field_map().get(spec.date_field)
        assert fieldspec is not None, (
            f"{spec.key}.date_field={spec.date_field!r} is not a declared field"
        )
        assert fieldspec.type == "date"


@pytest.mark.parametrize("spec", ALL_ENTITIES)
def test_annotations_really_produce_their_declared_fields(spec, db):
    """The end-to-end check: run the annotation, look for the columns.

    A `source` can name a real method and still not appear — the method may
    annotate something else entirely. Nothing short of executing it knows.
    """
    derived = [f.name for f in spec.fields if f.source]
    if not derived:
        return
    model = apps.get_model(*spec.model.split("."))
    queryset = spec.annotate_for(spec.base_queryset_for(model.objects))
    produced = set(queryset.query.annotations)
    missing = [name for name in derived if name not in produced]
    assert not missing, (
        f"{spec.key} declares {missing} as derived, but "
        f"{list(spec.annotate)} produced {sorted(produced)}"
    )


def test_the_ledger_declares_the_fields_money_questions_need():
    """A regression guard on the specific gap that started this work.

    "How many rents did we receive for August or are due?" is unanswerable
    without a paid/unpaid dimension and a summable amount.
    """
    spec = MANIFEST["ledger_entry"]
    fields = spec.field_map()
    assert fields["charge_state"].groupable
    assert fields["amount"].aggregatable
    assert fields["outstanding"].aggregatable
    assert spec.date_field == "due_date"
    assert "not_voided" in spec.base_queryset
