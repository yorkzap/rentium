from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rama", "0025_ramadocument_soft_delete")]

    operations = [
        migrations.AddField(
            model_name="ramaplanstep",
            name="step_id",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AddField(
            model_name="ramaplanstep",
            name="depends_on",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddConstraint(
            model_name="ramaplanstep",
            constraint=models.UniqueConstraint(
                condition=~models.Q(step_id=""),
                fields=("plan", "step_id"),
                name="rama_plan_step_id_unique",
            ),
        ),
    ]
