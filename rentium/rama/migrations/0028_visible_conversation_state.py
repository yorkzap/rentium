import uuid

import django.db.models.deletion
from django.db import migrations, models


def close_legacy_work(apps, schema_editor):
    """Do not let pre-rollout prose confirm old work under the new contract."""
    PendingPlan = apps.get_model("rama", "RamaPendingPlan")
    RamaTask = apps.get_model("rama", "RamaTask")
    task_ids = list(PendingPlan.objects.values_list("task_id", flat=True))
    RamaTask.objects.filter(id__in=[pk for pk in task_ids if pk]).exclude(
        status__in=["VERIFIED", "FAILED", "CANCELLED", "EXPIRED"]
    ).update(
        status="CANCELLED",
        outcome={
            "kind": "noop",
            "message": "Closed during the visible-conversation state rollout; nothing was executed.",
        },
    )
    PendingPlan.objects.all().delete()


class Migration(migrations.Migration):
    # PostgreSQL cannot ALTER RamaPendingPlan in the same transaction where
    # the rollout data step deletes rows with deferred FK trigger events.
    # Committing each phase also makes the intended stale-plan closure explicit.
    atomic = False

    dependencies = [
        ("events", "0002_notification_and_more"),
        ("rama", "0027_saved_workflows"),
    ]

    operations = [
        migrations.CreateModel(
            name="RamaConversation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("channel", models.CharField(choices=[("web", "Web"), ("telegram", "Telegram"), ("whatsapp", "WhatsApp"), ("system", "System")], default="web", max_length=20)),
                ("external_key", models.CharField(blank=True, db_index=True, default="", max_length=190)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("landlord", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rama_conversations", to="users.landlordprofile")),
            ],
        ),
        migrations.CreateModel(
            name="RamaEpisode",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("last_visible_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("ended_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("end_reason", models.CharField(blank=True, choices=[("IDLE", "Visible inactivity"), ("RESET", "Explicit reset"), ("ROLLOUT", "State-model rollout")], default="", max_length=20)),
                ("conversation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="episodes", to="rama.ramaconversation")),
            ],
            options={"ordering": ["started_at"]},
        ),
        migrations.AddField(
            model_name="ramaconversation",
            name="active_episode",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="rama.ramaepisode"),
        ),
        migrations.AddIndex(
            model_name="ramaconversation",
            index=models.Index(fields=["landlord", "channel", "external_key"], name="rama_conv_channel_idx"),
        ),
        migrations.AddConstraint(
            model_name="ramaepisode",
            constraint=models.UniqueConstraint(condition=models.Q(("ended_at__isnull", True)), fields=("conversation",), name="rama_one_open_episode"),
        ),
        migrations.CreateModel(
            name="RamaMessage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("direction", models.CharField(choices=[("INBOUND", "Inbound"), ("OUTBOUND", "Outbound")], max_length=10)),
                ("kind", models.CharField(choices=[("CHAT", "Chat"), ("NOTIFICATION", "Notification"), ("PLAN_PROMPT", "Plan prompt"), ("RECOVERY", "Recovery")], default="CHAT", max_length=20)),
                ("text", models.TextField(blank=True, default="")),
                ("role", models.CharField(blank=True, default="", max_length=20)),
                ("channel", models.CharField(blank=True, default="", max_length=20)),
                ("external_message_id", models.CharField(blank=True, default="", max_length=190)),
                ("semantic_payload", models.JSONField(blank=True, default=dict)),
                ("entity_refs", models.JSONField(blank=True, default=list)),
                ("delivery_status", models.CharField(choices=[("LOCAL", "Visible in local client"), ("SENT", "Sent"), ("FAILED", "Failed")], default="LOCAL", max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("attachment_batch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="messages", to="rama.ramaattachmentbatch")),
                ("conversation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="rama.ramaconversation")),
                ("episode", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="messages", to="rama.ramaepisode")),
                ("landlord", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rama_messages", to="users.landlordprofile")),
                ("reply_to", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="replies", to="rama.ramamessage")),
                ("source_event", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="rama_messages", to="events.domainevent")),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.AddIndex(
            model_name="ramamessage",
            index=models.Index(fields=["conversation", "episode", "created_at"], name="rama_msg_episode_idx"),
        ),
        migrations.AddIndex(
            model_name="ramamessage",
            index=models.Index(fields=["conversation", "external_message_id"], name="rama_msg_external_idx"),
        ),
        migrations.AddField(model_name="ramatask", name="active_prompt", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="prompted_tasks", to="rama.ramamessage")),
        migrations.AddField(model_name="ramatask", name="entity_refs", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="ramatask", name="episode", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="tasks", to="rama.ramaepisode")),
        migrations.AddField(model_name="ramatask", name="expires_at", field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="ramatask", name="source_message", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="tasks", to="rama.ramamessage")),
        migrations.AddField(model_name="ramatask", name="subject", field=models.CharField(blank=True, db_index=True, default="", max_length=80)),
        migrations.AlterField(model_name="ramatask", name="status", field=models.CharField(choices=[("RECEIVED", "Received"), ("NEEDS_INPUT", "Needs input"), ("READY", "Ready"), ("AWAITING_CONFIRMATION", "Awaiting confirmation"), ("EXECUTING", "Executing"), ("VERIFIED", "Verified"), ("FAILED", "Failed"), ("CANCELLED", "Cancelled"), ("SUSPENDED", "Suspended"), ("EXPIRED", "Expired")], db_index=True, default="RECEIVED", max_length=30)),
        migrations.AddField(model_name="ramapendingplan", name="episode", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="pending_plans", to="rama.ramaepisode")),
        migrations.AddField(model_name="ramapendingplan", name="expires_at", field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="ramapendingplan", name="plan_id", field=models.UUIDField(default=uuid.uuid4, editable=False, null=True)),
        migrations.AddField(model_name="ramapendingplan", name="prompt_message", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="pending_plans", to="rama.ramamessage")),
        migrations.RunPython(close_legacy_work, migrations.RunPython.noop),
        migrations.AlterField(model_name="ramapendingplan", name="plan_id", field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
    ]
