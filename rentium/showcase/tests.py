import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from rentium.showcase.services import known_city


@pytest.mark.django_db
def test_evergreen_city_exists_without_property_inventory():
    assert known_city("BC", "saanich") == {
        "city": "Saanich",
        "city_slug": "saanich",
        "province_code": "bc",
        "province_name": "British Columbia",
    }


@pytest.mark.django_db
def test_public_evergreen_city_returns_empty_market_instead_of_404():
    response = APIClient().get(
        reverse(
            "showcase_public:city",
            kwargs={"province": "bc", "city": "saanich"},
        )
    )

    assert response.status_code == 200
    assert response.data["city"] == "Saanich"
    assert response.data["facets"]["total"] == 0
    assert response.data["results"] == []


@pytest.mark.django_db
def test_public_listing_discovery_returns_evergreen_areas():
    response = APIClient().get(reverse("showcase_public:listings"))

    assert response.status_code == 200
    assert response.data["total"] == 0
    assert response.data["results"] == []
    assert any(
        area["province_code"] == "bc" and area["city_slug"] == "saanich"
        for area in response.data["areas"]
    )
