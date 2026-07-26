import django.db.models.deletion
from django.db import migrations
from django.db import models


def backfill_holding(apps, schema_editor):
    LedgerEntry = apps.get_model("ledger", "LedgerEntry")
    for entry in LedgerEntry.objects.filter(
        property__isnull=False, holding__isnull=True
    ).select_related("property"):
        if entry.property.holding_id:
            LedgerEntry.objects.filter(pk=entry.pk).update(
                holding_id=entry.property.holding_id
            )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0006_importbatch_stagedledgerentry"),
        ("properties", "0011_property_default_bills_included"),
    ]

    operations = [
        migrations.AddField(
            model_name="ledgerentry",
            name="holding",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Physical/legal property scope. Set directly for holding-wide "
                    "costs such as tax or mortgage; otherwise derived from property."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ledger_entries",
                to="properties.propertyholding",
            ),
        ),
        migrations.RunPython(backfill_holding, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="ledgerentry",
            index=models.Index(
                fields=["landlord", "holding", "effective_date"],
                name="ledger_holding_date_idx",
            ),
        ),
    ]
