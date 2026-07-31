# Invite email delivery tracking fields on Appointment.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0009_prospect_link_open_tracking"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="invite_email_status",
            field=models.CharField(
                choices=[
                    ("NONE", "No email"),
                    ("QUEUED", "Queued / sent to provider"),
                    ("DELIVERED", "Delivered to inbox"),
                    ("OPENED", "Email opened (pixel, optional)"),
                    ("BOUNCED", "Bounced"),
                    ("DROPPED", "Dropped / blocked"),
                    ("DEFERRED", "Deferred"),
                    ("FAILED", "Send failed"),
                ],
                db_index=True,
                default="NONE",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="appointment",
            name="invite_email_provider_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Provider message id (e.g. SendGrid x-message-id).",
                max_length=128,
            ),
        ),
        migrations.AddField(
            model_name="appointment",
            name="invite_email_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="invite_email_detail",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
