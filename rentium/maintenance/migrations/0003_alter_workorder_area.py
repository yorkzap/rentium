# Repoint WorkOrder.area from the retired Area model to PropertyArea.
#
# Drop-and-re-add rather than AlterField: the old Area used a UUID primary key
# and PropertyArea uses a bigint, so Postgres refuses the in-place cast
# ("cannot cast type uuid to bigint"). That is safe here precisely because the
# Area model never held a row — its seeding signals were never connected — so
# every area_id in this table is NULL and nothing is lost.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("maintenance", "0002_alter_workorder_contractor_phone"),
        ("properties", "0013_property_is_active_offering_propertyunit_and_more"),
    ]

    operations = [
        migrations.RemoveField(model_name="workorder", name="area"),
        migrations.AddField(
            model_name="workorder",
            name="area",
            field=models.ForeignKey(
                blank=True,
                help_text="Which space the issue is in. Blank = the room/unit itself.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="work_orders",
                to="properties.propertyarea",
            ),
        ),
    ]
