# Standalone Django bootstrap and CLI output.
# ruff: noqa: E402, INP001, T201
"""Seed one disposable browser portfolio; never touch production records."""

import json
import os
import uuid
from pathlib import Path

import django

django.setup()

from django.conf import settings
from django.utils import timezone

settings.CELERY_BROKER_URL = "memory://"
settings.CELERY_RESULT_BACKEND = "cache+memory://"
from rest_framework.authtoken.models import Token

from rentium.properties.models import Property
from rentium.rama.models import RamaPreferences
from rentium.rama.plan_runner import run_plan
from rentium.rama.plan_runner import save_single
from rentium.users.models import LandlordProfile
from rentium.users.models import User

if not str(settings.DATABASES["default"]["NAME"]).startswith("rentium_workflow"):
    message = "Use a disposable rentium_workflow database."
    raise SystemExit(message)
output = os.environ["WORKFLOW_DEMO_OUTPUT"]
user = User.objects.create_user(
    email=f"workflow-browser-{uuid.uuid4().hex}@example.invalid",
    name="Browser demo",
    user_type="LANDLORD",
    password=None,
)
owner = LandlordProfile.objects.create(user=user)
RamaPreferences.objects.create(
    landlord=owner,
    enabled=True,
    workflows_enabled=True,
    provider="mistral",
    model="mistral-small-latest",
    api_key="local-browser-fixture-no-provider-calls",
)
prop = Property.objects.create(
    landlord=owner,
    name="Browser Suite",
    address="10 Example Road",
    city="Victoria",
    province="bc",
    property_category="COMPLETE_UNIT",
    unit_type="OTHER",
)
plan = save_single(
    owner,
    uuid.uuid4(),
    "update_property",
    {"property_query": str(prop.pk), "name": "Reviewed Suite", "asking_rent": "925"},
)
plan.expires_at = timezone.now()
plan.save(update_fields=["expires_at"])
task_id = str(plan.task_id)
run_plan(plan, owner)
token, _ = Token.objects.get_or_create(user=user)
path = Path(output)
path.write_text(
    json.dumps(
        {
            "token": token.key,
            "email": user.email,
            "task_id": task_id,
            "property_id": str(prop.pk),
        },
    ),
)
path.chmod(0o600)
print("Created disposable workflow browser fixture.")
