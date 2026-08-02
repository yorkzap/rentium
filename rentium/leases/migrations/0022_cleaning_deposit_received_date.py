from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("leases", "0021_alter_leaseinviteevent_kind")]

    operations = [
        migrations.AddField(
            model_name="lease",
            name="cleaning_deposit_received_date",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="Cleaning Deposit Received On",
            ),
        ),
    ]
