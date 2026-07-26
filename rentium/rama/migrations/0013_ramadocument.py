import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0007_ledgerentry_holding"),
        ("properties", "0011_property_default_bills_included"),
        ("rama", "0012_ramapreferences_fsa_api_key_and_more"),
        ("users", "0006_alter_user_phone"),
    ]

    operations = [
        migrations.CreateModel(
            name="RamaDocument",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "portfolio_wide",
                    models.BooleanField(
                        default=False,
                        help_text="True only when the record genuinely concerns the whole portfolio.",
                    ),
                ),
                (
                    "original_file",
                    models.FileField(
                        max_length=500, upload_to="business_documents/inbox/%Y/%m/"
                    ),
                ),
                (
                    "archival_pdf",
                    models.FileField(
                        blank=True, default="", max_length=500, upload_to=""
                    ),
                ),
                ("original_filename", models.CharField(max_length=255)),
                (
                    "canonical_filename",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "media_type",
                    models.CharField(blank=True, default="", max_length=100),
                ),
                ("byte_size", models.PositiveBigIntegerField(default=0)),
                ("sha256", models.CharField(db_index=True, max_length=64)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("EXPENSE", "Expense / payable"),
                            ("NOTICE", "Notice"),
                            ("MORTGAGE", "Mortgage / financing"),
                            ("INSURANCE", "Insurance"),
                            ("LEASE", "Lease / tenancy"),
                            ("TAX", "Tax record"),
                            ("MAINTENANCE", "Maintenance"),
                            ("OTHER", "Other document"),
                        ],
                        default="OTHER",
                        max_length=20,
                    ),
                ),
                (
                    "expense_category",
                    models.CharField(blank=True, default="", max_length=20),
                ),
                (
                    "payment_state",
                    models.CharField(
                        choices=[
                            ("NOT_APPLICABLE", "Not applicable"),
                            ("PAID", "Paid"),
                            ("UNPAID", "Unpaid / not yet cleared"),
                            ("UNKNOWN", "Needs confirmation"),
                        ],
                        default="NOT_APPLICABLE",
                        max_length=20,
                    ),
                ),
                ("title", models.CharField(blank=True, default="", max_length=255)),
                ("issuer", models.CharField(blank=True, default="", max_length=200)),
                (
                    "reference_number",
                    models.CharField(blank=True, default="", max_length=120),
                ),
                (
                    "document_date",
                    models.DateField(blank=True, db_index=True, null=True),
                ),
                ("due_date", models.DateField(blank=True, null=True)),
                (
                    "amount",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        max_digits=12,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(0)],
                    ),
                ),
                ("currency", models.CharField(default="CAD", max_length=3)),
                ("ocr_text", models.TextField(blank=True, default="")),
                ("extracted_data", models.JSONField(blank=True, default=dict)),
                (
                    "classification_confidence",
                    models.DecimalField(decimal_places=4, default=0, max_digits=5),
                ),
                (
                    "match_confidence",
                    models.DecimalField(decimal_places=4, default=0, max_digits=5),
                ),
                ("clarification_question", models.TextField(blank=True, default="")),
                ("clarification_answer", models.TextField(blank=True, default="")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("QUEUED", "Queued"),
                            ("PROCESSING", "Processing"),
                            ("NEEDS_REVIEW", "Needs review"),
                            ("READY", "Ready to file"),
                            ("FILED", "Filed"),
                            ("FAILED", "Processing failed"),
                        ],
                        db_index=True,
                        default="QUEUED",
                        max_length=20,
                    ),
                ),
                ("failure_reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("filed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="rama_documents_created",
                        to="users.user",
                    ),
                ),
                (
                    "holding",
                    models.ForeignKey(
                        blank=True,
                        help_text="Physical/legal property this record concerns.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="documents",
                        to="properties.propertyholding",
                    ),
                ),
                (
                    "landlord",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="rama_documents",
                        to="users.landlordprofile",
                    ),
                ),
                (
                    "ledger_entry",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="source_document",
                        to="ledger.ledgerentry",
                    ),
                ),
                (
                    "property",
                    models.ForeignKey(
                        blank=True,
                        help_text="Optional rentable room/unit; holding is the normal filing scope.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="documents",
                        to="properties.property",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="RamaDocumentEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("UPLOADED", "Uploaded"),
                            ("OCR_COMPLETED", "OCR completed"),
                            ("CLASSIFIED", "Classified"),
                            ("CLARIFIED", "Clarified"),
                            ("FILED", "Filed"),
                            ("EXPENSE_POSTED", "Expense posted"),
                            ("FAILED", "Failed"),
                        ],
                        max_length=24,
                    ),
                ),
                ("detail", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="users.user",
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="rama.ramadocument",
                    ),
                ),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.AddConstraint(
            model_name="ramadocument",
            constraint=models.UniqueConstraint(
                fields=("landlord", "sha256"),
                name="rama_document_landlord_sha256_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="ramadocument",
            index=models.Index(
                fields=["landlord", "status", "-created_at"], name="rama_doc_status_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="ramadocument",
            index=models.Index(
                fields=["landlord", "holding", "document_date"],
                name="rama_doc_holding_idx",
            ),
        ),
    ]
