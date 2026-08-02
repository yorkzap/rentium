from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agenda", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="agendaevent",
            name="archived_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        )
    ]
