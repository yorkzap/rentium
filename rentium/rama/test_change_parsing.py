"""The `update` tool's change parser.

Reconstructed from the audit log: `update` had a 76% failure rate (53 errors in
70 calls), and 32 of those errors were the SAME correct call being rejected —

    changes='description=1 bedroom, living room, kitchen, washroom, private patio'

The parser split on every comma before looking for '=', so it read
'description=1 bedroom', then rejected 'living room' as a malformed clause. Any
free-text value containing a comma failed, which is most of them. The model was
doing nothing wrong.
"""

import pytest

from rentium.rama.domain_write import _parse_change_clauses
from rentium.rama.domain_write import _split_change_clauses

FIELDS = {"name", "description", "address", "city", "province", "postal_code", "status"}


def test_a_value_may_contain_commas():
    """The exact call from the log, which failed 32 times."""
    parsed, err = _parse_change_clauses(
        "description=1 bedroom, living room, kitchen, washroom, private patio",
        FIELDS,
    )
    assert err is None
    assert parsed == {
        "description": "1 bedroom, living room, kitchen, washroom, private patio"
    }


def test_several_changes_still_split():
    parsed, err = _parse_change_clauses(
        "name=Garden Suite, city=Victoria, province=bc", FIELDS
    )
    assert err is None
    assert parsed == {
        "name": "Garden Suite",
        "city": "Victoria",
        "province": "bc",
    }


def test_a_comma_value_followed_by_another_field():
    parsed, err = _parse_change_clauses(
        "description=1 bedroom, kitchen, private patio, city=Victoria", FIELDS
    )
    assert err is None
    assert parsed == {
        "description": "1 bedroom, kitchen, private patio",
        "city": "Victoria",
    }


def test_longest_field_name_wins_at_a_boundary():
    """`postal_code` must not be cut short by a prefix match."""
    parsed, err = _parse_change_clauses(
        "description=near the park, postal_code=V9A 1B5", FIELDS
    )
    assert err is None
    assert parsed["postal_code"] == "V9A 1B5"


def test_a_word_that_merely_looks_like_a_field_is_not_a_boundary():
    """'status' inside prose isn't a new clause unless it is followed by '='."""
    parsed, err = _parse_change_clauses(
        "description=good status, quiet street", FIELDS
    )
    assert err is None
    assert parsed == {"description": "good status, quiet street"}


def test_genuinely_unparseable_input_says_what_to_do():
    """A rejection has to be actionable — the old one just repeated the rule."""
    parsed, err = _parse_change_clauses("just some prose", FIELDS)
    assert parsed == {}
    assert err is not None
    assert "editable_fields" in err
    assert "example" in err
    assert "description" in err["editable_fields"]


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_input_is_no_changes_not_an_error(blank):
    parsed, err = _parse_change_clauses(blank, FIELDS)
    assert parsed == {}
    assert err is None


def test_split_falls_back_to_commas_when_no_fields_are_known():
    assert _split_change_clauses("a=1, b=2", set()) == ["a=1", "b=2"]
