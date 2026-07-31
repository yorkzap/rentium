"""Bills summary must name each utility — never bare '- Included in rent'."""

from __future__ import annotations

import pytest

from rentium.leases.models import Lease
from rentium.properties.models import Property

pytestmark = pytest.mark.django_db


def test_bills_summary_uses_utility_labels_not_empty_provider(landlord):
    prop = Property.objects.create(
        landlord=landlord,
        name="Basement Room",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    lease = Lease(
        landlord=landlord,
        property=prop,
        start_date="2026-08-01",
        total_rent=850,
        bills_included={
            "electricity": {
                "included": True,
                "provider": "BC Hydro",
                "category": "electricity",
                "tenant_responsibility": {},
                "notes": "",
            },
            "water": {
                "included": True,
                "provider": "",
                "category": "water",
                "tenant_responsibility": {},
                "notes": "",
            },
            "internet": {
                "included": False,
                "provider": "Telus",
                "category": "internet",
                "tenant_responsibility": {
                    "type": "percentage",
                    "value": 100,
                    "distribution": "equal",
                },
                "notes": "",
            },
        },
    )
    summary = lease.get_bills_summary()
    assert "Electricity (BC Hydro)" in summary
    assert "Water — included in rent" in summary
    assert "Internet (Telus)" in summary
    assert "tenant pays 100%" in summary
    # Regression: empty provider must not produce leading dash-only labels.
    assert " - Included" not in summary
    assert "; - " not in summary
