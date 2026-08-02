from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("leases", "0020_cleaning_fee_to_refundable_deposit")]

    operations = [
        migrations.AlterField(
            model_name="leaseinviteevent",
            name="kind",
            field=models.CharField(
                choices=[
                    ("SENT", "Invite sent"),
                    ("LINK_OPENED", "Invite link opened"),
                    ("LEASE_VIEWED", "Lease agreement viewed"),
                    ("ACCOUNT_LINKED", "Account linked"),
                    ("SIGNED", "Lease signed"),
                    ("DECLINED", "Lease declined"),
                    ("RESENT", "Invite resent"),
                ],
                db_index=True,
                max_length=30,
            ),
        )
    ]
