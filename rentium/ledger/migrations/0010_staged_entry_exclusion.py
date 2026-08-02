from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ledger", "0009_cleaning_deposits_are_refundable")]

    operations = [
        migrations.AddField(
            model_name="stagedledgerentry",
            name="excluded_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="stagedledgerentry",
            name="exclusion_reason",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
