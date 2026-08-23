from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rama", "0028_visible_conversation_state")]

    operations = [
        migrations.AddField(
            model_name="ramapreferences",
            name="telegram_orchestration_mode",
            field=models.CharField(
                choices=[
                    ("legacy", "Legacy"),
                    ("shadow", "V3 shadow"),
                    ("v3", "V3 trusted core"),
                ],
                default="legacy",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="ramapreferences",
            name="web_orchestration_mode",
            field=models.CharField(
                choices=[
                    ("legacy", "Legacy"),
                    ("shadow", "V3 shadow"),
                    ("v3", "V3 trusted core"),
                ],
                default="legacy",
                help_text="Per-channel RAMA rollout mode; legacy is the instant rollback.",
                max_length=12,
            ),
        ),
    ]
