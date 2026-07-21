# The unused "planner" seam becomes the General's model config (the smarter
# decision model is exactly what the General is), plus FSA fields — the CAF
# per-role model pyramid. Pure renames + additive fields; reversible.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rama", "0006_ramapreferences_planner_model_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="ramapreferences",
            old_name="planner_provider",
            new_name="general_provider",
        ),
        migrations.RenameField(
            model_name="ramapreferences",
            old_name="planner_model",
            new_name="general_model",
        ),
        migrations.AddField(
            model_name="ramapreferences",
            name="fsa_provider",
            field=models.CharField(
                blank=True,
                choices=[
                    ("xai", "xAI (Grok)"),
                    ("gemini", "Google Gemini"),
                    ("mistral", "Mistral AI"),
                    ("anthropic", "Anthropic (Claude)"),
                    ("openai", "OpenAI"),
                ],
                default="",
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="ramapreferences",
            name="fsa_model",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
    ]
