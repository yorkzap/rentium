from datetime import date

import pytest

from rentium.users.models import User
from rentium.users.tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _media_storage(settings, tmpdir) -> None:
    settings.MEDIA_ROOT = tmpdir.strpath


@pytest.fixture
def user(db) -> User:
    return UserFactory()


# --- Minimal landlord -> property -> lease graph, built through the real
# models so invariants stay honest. Used by ledger and attention tests.


@pytest.fixture
def landlord(db):
    from rentium.users.models import LandlordProfile

    return LandlordProfile.objects.create(user=UserFactory())


@pytest.fixture
def tenant(db):
    from rentium.users.models import TenantProfile

    return TenantProfile.objects.create(user=UserFactory())


@pytest.fixture
def bc_property(landlord):
    from rentium.properties.models import Property

    return Property.objects.create(
        landlord=landlord,
        name="Oak Ave Suite B",
        address="1234 Oak Ave",
        city="Victoria",
        province="BC",
        postal_code="V8V 1V1",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    )


@pytest.fixture
def bc_lease(landlord, bc_property):
    from rentium.leases.models import Lease

    return Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="850.00",
    )
