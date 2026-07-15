"""Add Appointment.public_token — the requester's capability link.

A unique field with a callable default can't be added in one ALTER (every
existing row would get the same value), so: add nullable, backfill each row
with its own uuid4, then tighten to unique + non-null.
"""

import uuid

from django.db import migrations, models


def backfill_tokens(apps, schema_editor):
    Appointment = apps.get_model("appointments", "Appointment")
    for appt in Appointment.objects.filter(public_token__isnull=True).only("id"):
        appt.public_token = uuid.uuid4()
        appt.save(update_fields=["public_token"])


class Migration(migrations.Migration):
    dependencies = [
        ("appointments", "0003_alter_appointment_contact_phone"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="public_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, null=True),
        ),
        migrations.RunPython(backfill_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="appointment",
            name="public_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
