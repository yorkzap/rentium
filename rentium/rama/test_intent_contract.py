from __future__ import annotations

import uuid

import pytest

from rentium.leases.models import RentAdjustment
from rentium.rama.intent_contract import contract_from_messages
from rentium.rama.intent_contract import contract_for_effects
from rentium.rama.intent_contract import validate_effect_set
from rentium.rama.intent_contract import validate_step
from rentium.rama.plan_runner import run_plan
from rentium.rama.plan_runner import save_single

pytestmark = pytest.mark.django_db


def _contract(lease_number: str = "RMT652523-C281") -> dict:
    return contract_from_messages(
        [
            "prepare preview",
            (
                "adjust the first month rent on lease "
                f"{lease_number} to be $400 only"
            ),
        ],
    )


def test_contract_survives_a_terse_follow_up_and_preserves_final_total():
    contract = _contract()

    assert contract["required_capability"] == "apply_rent_adjustment"
    assert contract["constraints"] == {
        "lease_number": "RMT652523-C281",
        "target_lease_total": "400.00",
    }


def test_prorated_rent_uses_the_requested_destination_not_the_old_amount():
    contract = contract_from_messages(
        [
            (
                "For lease RMT698948-2EA3, change the prorated rent that was "
                "set at $1,716.13 to $1,900 instead"
            ),
        ],
    )

    assert contract["required_capability"] == "apply_rent_adjustment"
    assert contract["constraints"]["lease_number"] == "RMT698948-2EA3"
    assert contract["constraints"]["target_lease_total"] == "1900.00"


@pytest.mark.parametrize("wrong_tool", ["update_lease", "post_one_off_charge"])
def test_semantically_different_tools_are_rejected(wrong_tool):
    errors = validate_step(
        _contract(),
        wrong_tool,
        {"lease_number": "RMT652523-C281"},
    )

    assert errors
    assert "requested apply_rent_adjustment" in errors[0]


def test_model_arithmetic_cannot_replace_a_requested_final_total():
    errors = validate_step(
        _contract(),
        "apply_rent_adjustment",
        {
            "lease_number": "RMT652523-C281",
            "amount": "1700.00",
            "adjustment_type": "DISCOUNT",
        },
    )

    assert any("target_lease_total=400.00" in error for error in errors)
    assert any("model-computed amount" in error for error in errors)


def test_confirmation_rechecks_the_persisted_intent_contract(landlord, bc_lease):
    contract = _contract(bc_lease.lease_number)
    plan = save_single(
        landlord,
        uuid.uuid4(),
        "apply_rent_adjustment",
        {
            "lease_number": bc_lease.lease_number,
            "target_lease_total": "400.00",
            "effective_date": str(bc_lease.start_date),
            "is_recurring": "0",
        },
        contract,
    )
    # Simulate corruption or legacy code replacing the validated final-state
    # arguments after preview. Confirmation must still refuse it.
    step = plan.steps.get()
    step.arguments = {
        "lease_number": bc_lease.lease_number,
        "amount": "1700.00",
        "adjustment_type": "DISCOUNT",
        "effective_date": str(bc_lease.start_date),
        "is_recurring": "0",
    }
    step.save(update_fields=["arguments"])

    result = run_plan(plan, landlord)

    assert result["status"] == "failed"
    assert result["failed"][0]["code"] == "INTENT_MISMATCH"
    assert not RentAdjustment.objects.filter(
        lease_tenant__lease=bc_lease,
    ).exists()


def test_batch_contract_requires_every_effect_exactly_once():
    contract = contract_for_effects(
        "set both rents",
        [
            {
                "capability": "apply_rent_adjustment",
                "constraints": {
                    "lease_number": "RMT-A",
                    "effective_date": "2026-08-01",
                    "target_lease_total": "400.00",
                },
            },
            {
                "capability": "apply_rent_adjustment",
                "constraints": {
                    "lease_number": "RMT-B",
                    "effective_date": "2026-08-04",
                    "target_lease_total": "1900.00",
                },
            },
        ],
    )
    one_step = [
        {
            "tool": "apply_rent_adjustment",
            "arguments": {
                "lease_number": "RMT-A",
                "effective_date": "2026-08-01",
                "target_lease_total": "400.00",
            },
        }
    ]

    errors = validate_effect_set(contract, one_step)

    assert any("RMT-B" in error and "found 0" in error for error in errors)
