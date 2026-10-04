"""Create a disposable browser-test portfolio in an explicitly named demo DB.

Run with django settings configured and MONEY_DEMO_OUTPUT pointing to a JSON
file. Every run creates a new account; it never deletes ledger history.
"""

import json
import os
import uuid
from datetime import date, timedelta
from pathlib import Path

import django

django.setup()

from django.conf import settings  # noqa: E402
from django.db import transaction  # noqa: E402
from rest_framework.authtoken.models import Token  # noqa: E402
from rentium.ledger import services  # noqa: E402
from rentium.ledger.models import EntryType  # noqa: E402
from rentium.properties.models import Property, PropertyHolding, PropertyUnit  # noqa: E402
from rentium.users.models import LandlordProfile, TenantProfile, User  # noqa: E402

if not str(settings.DATABASES["default"]["NAME"]).startswith("rentium_money"):
    raise SystemExit(
        "Refusing: use a separate database named rentium_money_demo (or with that prefix)."
    )
output = os.environ.get("MONEY_DEMO_OUTPUT")
if not output:
    raise SystemExit("Set MONEY_DEMO_OUTPUT to a temporary JSON file path.")

with transaction.atomic():
    suffix = uuid.uuid4().hex[:12]
    owner = User.objects.create_user(
        email=f"money-demo-{suffix}@example.invalid",
        password=None,
        user_type="LANDLORD",
        name="Demo owner",
    )
    landlord = LandlordProfile.objects.create(user=owner)
    tenant = TenantProfile.objects.create(
        user=User.objects.create_user(
            email=f"tenant-demo-{suffix}@example.invalid",
            password=None,
            user_type="TENANT",
        )
    )
    holding = PropertyHolding.objects.create(
        landlord=landlord, name="Demo house", address="10 Example Road", city="Victoria"
    )
    unit = PropertyUnit.objects.create(
        landlord=landlord, holding=holding, name="Main floor", rental_mode="WHOLE_UNIT"
    )
    prop = Property.objects.create(
        landlord=landlord,
        holding=holding,
        unit=unit,
        name="Demo main floor",
        address="10 Example Road",
        city="Victoria",
        province="BC",
        postal_code="V8V 1V1",
        property_category="COMPLETE_UNIT",
    )
    today = date.today()
    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=tenant,
        property=prop,
        amount="500",
        entry_type=EntryType.RENT_CHARGE,
        due_date=today.replace(day=1),
        description="Demo rent",
    )
    expense, _ = services.post_expense(
        landlord=landlord,
        holding=holding,
        amount="75",
        category="UTILITIES",
        description="Water bill",
        incurred_date=today.replace(day=1) - timedelta(days=1),
    )
    token, _ = Token.objects.get_or_create(user=owner)
    payload = {
        "token": token.key,
        "email": owner.email,
        "charge_id": str(charge.pk),
        "expense_id": str(expense.pk),
        "date": str(today),
        "csv": f"Date,Description,Amount\n{today},Demo rent,500\n{today},Water bill,-75\n",
    }
Path(output).write_text(json.dumps(payload))
Path(output).chmod(0o600)
print("Created a disposable money-workflow portfolio.")
