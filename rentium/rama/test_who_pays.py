"""Answering "who pays for this?" from the record, not from policy.

    landlord: who will pay it?
    RAMA:     The Constitution is silent on who pays routine maintenance vs
              tenant-caused damage. I'll propose an amendment...

    landlord: i mean whose deposit will it be deducted from?
    RAMA:     ...proposes: "Tenant-caused damage ... may be deducted from
              their security deposit after move-out."

Two failures, one worse than the other.

The first is familiar: the answer was already recorded on the work order
(responsible tenant, chargeable, the claim raised) and none of it was in the
read payload, so RAMA reached for policy because it could not see data.

The second is dangerous. That sentence is not true under the BC RTA, and the
Constitution is text RAMA reads back as policy and acts on — so enshrining it
would have had RAMA repeat it confidently forever, on the one topic where
being wrong costs double the deposit.
"""

from datetime import date

import pytest

from rentium.maintenance.models import WorkOrder
from rentium.properties.models import Property
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit
from rentium.rama import registry
from rentium.rama.constitution import unlawful_deposit_language

pytestmark = pytest.mark.django_db


@pytest.fixture
def job(landlord, tenant):
    from rentium.leases.models import Lease

    holding = PropertyHolding.objects.create(
        landlord=landlord, name="950 McKenzie Ave",
        address="950 McKenzie Ave", city="Victoria",
    )
    unit = PropertyUnit.objects.create(
        landlord=landlord, holding=holding, name="Basement",
        rental_mode=PropertyUnit.RentalMode.BY_ROOM,
    )
    room = Property.objects.create(
        landlord=landlord, holding=holding, unit=unit, name="Room C",
        address="950 McKenzie Ave", city="Victoria", province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    lease = Lease.objects.create(
        landlord=landlord, property=room,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE, start_date=date.today(),
        is_month_to_month=True, total_rent="850.00",
    )
    lease.lease_tenants.create(tenant=tenant, rent_amount="850.00")
    wo = WorkOrder.objects.create(
        unit=unit, title="Shower knob broken", description="Knob spins free.",
        category="PLUMBING", priority="HIGH", cost="19.78",
    )
    return {"wo": wo, "tenant": tenant, "unit": unit}


def _rows(landlord):
    return registry.execute(
        "list_work_orders", {"include_closed": "yes"}, landlord=landlord
    )["work_orders"]


# ------------------------------------------------- answer from the record
def test_an_unattributed_job_says_the_landlord_pays(landlord, job):
    row = _rows(landlord)[0]
    assert row["responsible_tenant"] is None
    assert "landlord" in row["who_pays"].lower()
    assert "attribute_work_order" in row["who_pays"]


def test_a_chargeable_job_names_the_tenant_and_the_amount(landlord, job):
    registry.execute(
        "attribute_work_order",
        {"work_order_id": str(job["wo"].pk), "tenant": job["tenant"].user.name,
         "chargeable": "yes", "confirm": "yes"},
        landlord=landlord,
    )
    row = _rows(landlord)[0]

    assert row["responsible_tenant"] == job["tenant"].user.name
    assert row["tenant_chargeable"] is True
    assert "19.78" in row["who_pays"]


def test_the_answer_says_it_is_not_a_deposit_deduction(landlord, job):
    """The landlord's actual follow-up was "whose deposit?". The answer has to
    carry the constraint, not just a name."""
    registry.execute(
        "attribute_work_order",
        {"work_order_id": str(job["wo"].pk), "tenant": job["tenant"].user.name,
         "chargeable": "yes", "confirm": "yes"},
        landlord=landlord,
    )
    who = _rows(landlord)[0]["who_pays"].lower()

    assert "not taken from their deposit" in who
    assert "written" in who or "rtb" in who


def test_blame_without_charging_says_the_landlord_is_paying(landlord, job):
    registry.execute(
        "attribute_work_order",
        {"work_order_id": str(job["wo"].pk), "tenant": job["tenant"].user.name,
         "confirm": "yes"},
        landlord=landlord,
    )
    who = _rows(landlord)[0]["who_pays"]

    assert job["tenant"].user.name in who
    assert "landlord is paying" in who


def test_a_shared_space_job_reports_where_it_is(landlord, job):
    """It rendered as "" — wo.property is null for shared space."""
    assert _rows(landlord)[0]["property"] == "Basement (shared)"


# ------------------------------------------- refuse to enshrine bad law
def test_the_amendment_rama_proposed_is_refused(landlord):
    out = registry.execute(
        "amend_constitution",
        {
            "key": "balances",
            "new_body_md": (
                "Tenant-caused damage is charged to the tenant's ledger and "
                "may be deducted from their security deposit after move-out."
            ),
        },
        landlord=landlord,
    )
    assert "error" in out
    assert "suggested_wording" in out
    assert "in writing" in out["suggested_wording"].lower()


@pytest.mark.parametrize(
    "text",
    [
        "We keep the deposit for any damage.",
        "Cleaning costs are deducted from the security deposit.",
        "The landlord may withhold the pet damage deposit for repairs.",
    ],
)
def test_unilateral_deposit_language_is_caught(text):
    assert unlawful_deposit_language(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        # The subject is not banned — only asserting it without the condition.
        "Damage is deducted from the deposit only if the tenant agrees in "
        "writing or the RTB orders it.",
        "We apply to the RTB within 15 days to claim against the deposit.",
        "Deposits are returned in full within 15 days.",
        "Routine maintenance is paid from the operating account.",
        "",
    ],
)
def test_lawful_wording_passes(text):
    assert unlawful_deposit_language(text) is None


def test_a_lawful_amendment_still_previews(landlord):
    out = registry.execute(
        "amend_constitution",
        {
            "key": "balances",
            "new_body_md": (
                "Tenant-caused damage is charged to the tenant. It comes out "
                "of their deposit only with their written agreement or an RTB "
                "order."
            ),
        },
        landlord=landlord,
    )
    assert out.get("needs_confirm") is True
    assert "error" not in out
