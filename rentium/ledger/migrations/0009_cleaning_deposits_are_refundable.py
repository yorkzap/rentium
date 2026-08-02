from django.db import migrations


OLD_KINDS = {
    "cleaning_fee_lease": "cleaning_deposit_lease",
    "cleaning_fee_individual": "cleaning_deposit_individual",
}


def make_cleaning_deposits_refundable(apps, schema_editor):
    LedgerEntry = apps.get_model("ledger", "LedgerEntry")
    rows = LedgerEntry.objects.filter(
        entry_type="FEE_CHARGE",
        metadata__kind__in=list(OLD_KINDS),
    )
    for row in rows.iterator():
        metadata = dict(row.metadata or {})
        metadata["kind"] = OLD_KINDS[metadata["kind"]]
        key = row.idempotency_key
        if key:
            key = key.replace("cleaning_fee_", "cleaning_deposit_", 1)
        LedgerEntry.objects.filter(pk=row.pk).update(
            entry_type="DEPOSIT_CHARGE",
            metadata=metadata,
            idempotency_key=key,
        )


def restore_cleaning_fees(apps, schema_editor):
    LedgerEntry = apps.get_model("ledger", "LedgerEntry")
    reverse_kinds = {value: key for key, value in OLD_KINDS.items()}
    rows = LedgerEntry.objects.filter(
        entry_type="DEPOSIT_CHARGE",
        metadata__kind__in=list(reverse_kinds),
    )
    for row in rows.iterator():
        metadata = dict(row.metadata or {})
        metadata["kind"] = reverse_kinds[metadata["kind"]]
        key = row.idempotency_key
        if key:
            key = key.replace("cleaning_deposit_", "cleaning_fee_", 1)
        LedgerEntry.objects.filter(pk=row.pk).update(
            entry_type="FEE_CHARGE",
            metadata=metadata,
            idempotency_key=key,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("leases", "0020_cleaning_fee_to_refundable_deposit"),
        ("ledger", "0008_holdingfinancials_holdingvaluation_holdingmortgage_and_more"),
    ]

    operations = [
        migrations.RunPython(
            make_cleaning_deposits_refundable,
            reverse_code=restore_cleaning_fees,
        )
    ]
