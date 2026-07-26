"""Unit and hierarchy endpoints.

The hierarchy endpoint is the shape the dashboard should read: address -> unit
-> live offerings. Reading the flat listing list is what made a 9-unit
portfolio look like 14 rooms.
"""

from datetime import date

import pytest
from rest_framework.test import APIClient

from rentium.leases.models import Lease
from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(landlord):
    api = APIClient()
    api.force_authenticate(user=landlord.user)
    return api


@pytest.fixture
def floor(landlord):
    """One floor, let whole, with a parked room listing from a previous mode."""
    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="5654 McCaughey Street",
        address="5654 McCaughey Street",
        city="Regina",
    )
    unit = PropertyUnit.objects.create(
        landlord=landlord,
        holding=holding,
        name="Main Floor",
        unit_type=PropertyUnit.UnitType.MAIN_FLOOR,
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
        layout_complete=True,
    )
    live = Property.objects.create(
        landlord=landlord,
        holding=holding,
        unit=unit,
        name="McCaughey Main Floor",
        address="5654 McCaughey Street",
        city="Regina",
        province="sk",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.MAIN_FLOOR,
        bedrooms=3,
    )
    parked = Property.objects.create(
        landlord=landlord,
        holding=holding,
        unit=unit,
        name="McCaughey Room 2",
        address="5654 McCaughey Street",
        city="Regina",
        province="sk",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        is_active_offering=False,
    )
    master = PropertyArea.objects.create(
        unit=unit,
        name="Master Bedroom",
        area_type=PropertyArea.AreaType.BEDROOM,
        kind=PropertyArea.Kind.PRIVATE,
    )
    ensuite = PropertyArea.objects.create(
        unit=unit,
        name="Master Ensuite",
        area_type=PropertyArea.AreaType.BATHROOM,
        kind=PropertyArea.Kind.PRIVATE,
    )
    ensuite.serves_areas.set([master])
    PropertyArea.objects.create(
        unit=unit,
        name="Laundry",
        area_type=PropertyArea.AreaType.LAUNDRY,
        is_seeded_default=True,
    )
    return {"unit": unit, "live": live, "parked": parked, "holding": holding}


# --------------------------------------------------------------- hierarchy
def test_hierarchy_returns_address_unit_offerings(client, floor):
    resp = client.get("/api/properties/hierarchy/")
    assert resp.status_code == 200

    holdings = resp.json()["holdings"]
    assert len(holdings) == 1
    units = holdings[0]["units"]
    assert len(units) == 1
    assert units[0]["name"] == "Main Floor"
    assert [o["name"] for o in units[0]["offerings"]] == ["McCaughey Main Floor"]


def test_hierarchy_hides_parked_offerings_unless_asked(client, floor):
    default = client.get("/api/properties/hierarchy/").json()
    assert len(default["holdings"][0]["units"][0]["offerings"]) == 1

    with_inactive = client.get(
        "/api/properties/hierarchy/?include_inactive=true"
    ).json()
    names = {
        o["name"] for o in with_inactive["holdings"][0]["units"][0]["offerings"]
    }
    assert names == {"McCaughey Main Floor", "McCaughey Room 2"}


def test_layout_excludes_seeded_scaffolding(client, floor):
    """A placeholder "Laundry" must not be reported as recorded layout."""
    units = client.get("/api/properties/hierarchy/").json()["holdings"][0]["units"]
    labels = {a["label"] for a in units[0]["layout"]}

    assert labels == {"Master Bedroom", "Master Ensuite"}
    assert units[0]["layout_summary"]["bedrooms"] == 1
    assert units[0]["layout_summary"]["bathrooms"] == 1


def test_layout_reports_which_bedrooms_a_bathroom_serves(client, floor):
    units = client.get("/api/properties/hierarchy/").json()["holdings"][0]["units"]
    ensuite = next(a for a in units[0]["layout"] if a["label"] == "Master Ensuite")

    assert [s["label"] for s in ensuite["serves"]] == ["Master Bedroom"]


def test_unknown_layout_reads_as_unknown_not_as_zero(client, landlord, floor):
    """A blank layout must never look like "no bedrooms"."""
    bare = PropertyUnit.objects.create(
        landlord=landlord,
        holding=floor["holding"],
        name="Basement",
        missing_layout_notes="Nothing was ever recorded for this basement.",
    )
    resp = client.get(f"/api/properties/units/{bare.pk}/").json()

    assert resp["layout_summary"]["bedrooms"] is None
    assert resp["layout_summary"]["complete"] is False
    assert resp["layout_summary"]["unknown"]


# ------------------------------------------------------------ listing list
def test_property_list_hides_parked_listings_by_default(client, floor):
    names = {p["name"] for p in client.get("/api/properties/").json()}
    assert names == {"McCaughey Main Floor"}

    all_names = {
        p["name"] for p in client.get("/api/properties/?include_inactive=true").json()
    }
    assert all_names == {"McCaughey Main Floor", "McCaughey Room 2"}


def test_property_list_carries_unit_context(client, floor):
    row = client.get("/api/properties/").json()[0]
    assert row["unit_name"] == "Main Floor"
    assert row["rental_mode"] == PropertyUnit.RentalMode.WHOLE_UNIT
    assert row["holding_name"] == "5654 McCaughey Street"


# ------------------------------------------------------------ rental mode
def test_rental_mode_preview_writes_nothing(client, floor):
    resp = client.post(
        f"/api/properties/units/{floor['unit'].pk}/rental_mode_preview/",
        {"rental_mode": "BY_ROOM"},
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["will_park"] == ["McCaughey Main Floor"]
    assert body["will_reactivate"] == ["McCaughey Room 2"]

    floor["unit"].refresh_from_db()
    assert floor["unit"].rental_mode == PropertyUnit.RentalMode.WHOLE_UNIT


def test_set_rental_mode_parks_and_reactivates(client, floor):
    resp = client.post(
        f"/api/properties/units/{floor['unit'].pk}/set_rental_mode/",
        {"rental_mode": "BY_ROOM"},
        format="json",
    )
    assert resp.status_code == 200

    floor["live"].refresh_from_db()
    floor["parked"].refresh_from_db()
    assert floor["live"].is_active_offering is False
    assert floor["parked"].is_active_offering is True


def test_set_rental_mode_conflicts_while_a_lease_is_live(client, floor):
    Lease.objects.create(
        landlord=floor["live"].landlord,
        property=floor["live"],
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="1800.00",
    )

    resp = client.post(
        f"/api/properties/units/{floor['unit'].pk}/set_rental_mode/",
        {"rental_mode": "BY_ROOM"},
        format="json",
    )
    assert resp.status_code == 409
    assert "live leases" in resp.json()["error"]


def test_patching_rental_mode_directly_is_refused(client, floor):
    """A silent PATCH would leave listings pointing at the wrong mode."""
    resp = client.patch(
        f"/api/properties/units/{floor['unit'].pk}/",
        {"rental_mode": "BY_ROOM"},
        format="json",
    )
    assert resp.status_code == 400
    assert "rental_mode" in resp.json()

    floor["unit"].refresh_from_db()
    assert floor["unit"].rental_mode == PropertyUnit.RentalMode.WHOLE_UNIT


# ---------------------------------------------------------------- isolation
def test_units_are_scoped_to_their_owner(floor, db):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    stranger = LandlordProfile.objects.create(user=UserFactory())
    api = APIClient()
    api.force_authenticate(user=stranger.user)

    assert api.get("/api/properties/units/").json() == []
    assert api.get(f"/api/properties/units/{floor['unit'].pk}/").status_code == 404
    assert api.get("/api/properties/hierarchy/").json()["holdings"] == []
