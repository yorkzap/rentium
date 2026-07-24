from django.db import migrations
from django.db import models
from django.db.models import Count


def mark_existing_common_areas(apps, schema_editor):
    PropertyArea = apps.get_model("properties", "PropertyArea")
    ids = (
        PropertyArea.objects.annotate(shared_count=Count("shared_by"))
        .filter(shared_count__gte=2)
        .values_list("pk", flat=True)
    )
    PropertyArea.objects.filter(pk__in=list(ids)).update(is_group_common=True)


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0011_property_default_bills_included"),
    ]

    operations = [
        migrations.AddField(
            model_name="propertyarea",
            name="is_group_common",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    "This area belongs to the whole property group. Membership "
                    "is synchronized automatically as rooms join, move, or leave."
                ),
                verbose_name="Group Common Area",
            ),
        ),
        migrations.RunPython(
            mark_existing_common_areas,
            migrations.RunPython.noop,
        ),
    ]
