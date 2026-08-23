from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("rama", "0030_evidence_evals_certification_knowledge")]

    operations = [
        migrations.AddConstraint(
            model_name="ramaknowledgesource",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(kind="OFFICIAL", landlord__isnull=True)
                    | models.Q(kind="PRIVATE", landlord__isnull=False)
                ),
                name="rama_knowledge_scope_matches_kind",
            ),
        )
    ]
