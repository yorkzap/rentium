from django.db import migrations, models


def rename_legacy_payment_type(apps, schema_editor):
    Payment = apps.get_model("leases", "Payment")
    Payment.objects.filter(payment_type="CLEANING_FEE").update(
        payment_type="CLEANING_DEPOSIT"
    )


def restore_legacy_payment_type(apps, schema_editor):
    Payment = apps.get_model("leases", "Payment")
    Payment.objects.filter(payment_type="CLEANING_DEPOSIT").update(
        payment_type="CLEANING_FEE"
    )


class Migration(migrations.Migration):
    dependencies = [("leases", "0019_leaseinviteevent")]

    operations = [
        migrations.RenameField(
            model_name="lease",
            old_name="cleaning_fee",
            new_name="cleaning_deposit",
        ),
        migrations.RenameField(
            model_name="leasetenant",
            old_name="cleaning_fee",
            new_name="cleaning_deposit",
        ),
        migrations.RenameField(
            model_name="leasetenant",
            old_name="cleaning_fee_paid",
            new_name="cleaning_deposit_paid",
        ),
        migrations.RunPython(
            rename_legacy_payment_type,
            reverse_code=restore_legacy_payment_type,
        ),
        migrations.AlterField(
            model_name="lease",
            name="cleaning_deposit",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text=(
                    "Refundable cleaning deposit for the lease; individual roommate "
                    "deposits may be recorded on LeaseTenant"
                ),
                max_digits=10,
                verbose_name="Cleaning Deposit (Overall Lease)",
            ),
        ),
        migrations.AlterField(
            model_name="leasetenant",
            name="cleaning_deposit",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text=(
                    "Cleaning deposit charged specifically to this tenant "
                    "(for roommate leases)"
                ),
                max_digits=10,
                verbose_name="Individual Cleaning Deposit",
            ),
        ),
        migrations.AlterField(
            model_name="leasetenant",
            name="cleaning_deposit_paid",
            field=models.BooleanField(
                default=False,
                verbose_name="Cleaning Deposit Paid",
            ),
        ),
        migrations.AlterField(
            model_name="payment",
            name="payment_type",
            field=models.CharField(
                choices=[
                    ("RENT", "Rent Payment"),
                    ("SECURITY_DEPOSIT", "Security Deposit"),
                    ("PET_DEPOSIT", "Pet Deposit"),
                    ("CLEANING_DEPOSIT", "Cleaning Deposit"),
                    ("LATE_FEE", "Late Fee"),
                    ("UTILITY", "Utility Payment"),
                    ("MAINTENANCE", "Maintenance Fee/Chargeback"),
                    ("OTHER", "Other"),
                ],
                max_length=20,
                verbose_name="Payment Type",
            ),
        ),
    ]
