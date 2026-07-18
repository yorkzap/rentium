"""Add Appointment.public_token — the requester's capability link.

A unique field with a callable default can't be added in one ALTER (every
existing row would get the same value). Django evaluates `default=uuid.uuid4`
once when generating the ADD COLUMN default, so a nullable AddField with that
default still stamps every row with the *same* uuid — then the unique index
fails. Correct sequence: add as pure NULL (no default), backfill each row,
then tighten to unique + non-null with a callable default for new rows.
"""

import uuid

from django.db import migrations, models


def backfill_tokens(apps, schema_editor):
    Appointment = apps.get_model("appointments", "Appointment")
    # Assign a fresh token to every row that still lacks a unique value
    # (null from the AddField, or a collision from a prior failed attempt).
    seen: set[uuid.UUID] = set()
    for appt in Appointment.objects.only("id", "public_token").iterator():
        token = appt.public_token
        if token is None or token in seen:
            token = uuid.uuid4()
            while token in seen:
                token = uuid.uuid4()
            appt.public_token = token
            appt.save(update_fields=["public_token"])
        seen.add(token)


class Migration(migrations.Migration):
    dependencies = [
        ("appointments", "0003_alter_appointment_contact_phone"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="public_token",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(backfill_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="appointment",
            name="public_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
