from __future__ import annotations

import pytest

from rentium.maintenance.models import WorkOrder
from rentium.properties.models import Property
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit
from rentium.rama.fact_projections import ledger_account_position
from rentium.rama.fact_projections import maintenance_case_status
from rentium.rama.fact_projections import property_structure_and_offerings

pytestmark = pytest.mark.django_db


def test_property_projection_reports_incomplete_layout_as_partial(landlord):
    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="Evidence House",
        address="123 Evidence Street",
        city="Victoria",
    )
    unit = PropertyUnit.objects.create(
        landlord=landlord,
        holding=holding,
        name="Garden Suite",
        layout_complete=False,
        missing_layout_notes="Bathroom count has not been recorded.",
    )
    Property.objects.create(
        landlord=landlord,
        holding=holding,
        unit=unit,
        name="Garden Suite",
        address=holding.address,
        city=holding.city,
        province="BC",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    )

    packet = property_structure_and_offerings(holding)

    assert packet.coverage == "PARTIAL"
    assert packet.missing[0].key.endswith(".layout")
    assert packet.metadata["units"][0]["offerings"][0]["name"] == "Garden Suite"
    assert not packet.permits_negative("no_units")


def test_empty_ledger_projection_can_certify_exact_absence(landlord):
    packet = ledger_account_position(landlord)

    assert packet.coverage == "COMPLETE"
    assert packet.metadata["outstanding_now"] == "0.00"
    assert packet.permits_negative("no_outstanding_now")


def test_maintenance_projection_preserves_case_status(landlord, bc_property):
    work_order = WorkOrder.objects.create(
        property=bc_property,
        reported_by=landlord.user,
        origin=WorkOrder.Origin.LANDLORD,
        title="Leaking faucet",
        description="The kitchen faucet drips.",
        priority=WorkOrder.Priority.MEDIUM,
    )

    packet = maintenance_case_status(work_order)

    assert packet.coverage == "COMPLETE"
    assert packet.metadata["status"] == WorkOrder.Status.NEW
    assert packet.metadata["place"] == bc_property.name
