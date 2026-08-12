from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("leases", "0027_alter_leaseinviteevent_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="rentadjustment",
            name="target_amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text=(
                    "Optional final rent amount. When set, this adjustment "
                    "overrides earlier proration/discount arithmetic for the period."
                ),
                max_digits=10,
                null=True,
                verbose_name="Target Amount",
            ),
        ),
    ]
