import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rama", "0026_plan_step_dependencies")]

    operations = [
        migrations.CreateModel(
            name="RamaSavedWorkflow",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=120)),
                ("version", models.PositiveIntegerField(default=1)),
                ("parameter_schema", models.JSONField(blank=True, default=dict)),
                ("steps", models.JSONField(default=list)),
                ("capability_contract_version", models.CharField(default="landlord-v1", max_length=40)),
                ("archived_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_from_task", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="saved_workflows", to="rama.ramatask")),
                ("landlord", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rama_saved_workflows", to="users.landlordprofile")),
            ],
            options={"ordering": ["name", "-version"]},
        ),
        migrations.AddConstraint(
            model_name="ramasavedworkflow",
            constraint=models.UniqueConstraint(
                condition=models.Q(archived_at__isnull=True),
                fields=("landlord", "name"),
                name="rama_saved_workflow_live_name_unique",
            ),
        ),
    ]
