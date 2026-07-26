# Repoint the inspection area FKs from the retired Area model to PropertyArea.
#
# Drop-and-re-add rather than AlterField: the old Area used a UUID primary key
# and PropertyArea uses a bigint, so Postgres refuses the in-place cast
# ("cannot cast type uuid to bigint"). Safe here because the Area model never
# held a row — its seeding signals were never connected — so both tables are
# empty and every area_id is NULL.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("leases", "0016_leaselandlordsignatory_and_more"),
        ("properties", "0013_property_is_active_offering_propertyunit_and_more"),
    ]

    operations = [
        migrations.RemoveField(model_name="areaconditionstate", name="area"),
        migrations.AddField(
            model_name="areaconditionstate",
            name="area",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="condition_state",
                to="properties.propertyarea",
                # Nullable only for the duration of the AddField; the model
                # declares it required and both tables are empty.
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="areaconditionstate",
            name="area",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="condition_state",
                to="properties.propertyarea",
            ),
        ),
        migrations.RemoveField(model_name="inspectionitem", name="area"),
        migrations.AddField(
            model_name="inspectionitem",
            name="area",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="inspection_items",
                to="properties.propertyarea",
            ),
        ),
    ]
