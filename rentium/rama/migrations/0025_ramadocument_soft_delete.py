# Soft-delete / trash for RamaDocument + conditional unique sha256.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rama", "0024_document_library_phase_a"),
    ]

    operations = [
        migrations.AddField(
            model_name="ramadocument",
            name="deleted_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddIndex(
            model_name="ramadocument",
            index=models.Index(
                fields=["landlord", "deleted_at", "-created_at"],
                name="rama_doc_trash_idx",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="ramadocument",
            name="rama_document_landlord_sha256_unique",
        ),
        migrations.AddConstraint(
            model_name="ramadocument",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True)),
                fields=("landlord", "sha256"),
                name="rama_document_landlord_sha256_unique",
            ),
        ),
    ]
