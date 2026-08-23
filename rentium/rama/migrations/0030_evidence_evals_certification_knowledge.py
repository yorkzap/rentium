import uuid

import django.contrib.postgres.search
import django.db.models.deletion
import pgvector.django
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rama", "0029_rama_orchestration_modes")]

    operations = [
        pgvector.django.VectorExtension(),
        migrations.CreateModel(
            name="RamaEvalRun",
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
                ("provider", models.CharField(max_length=40)),
                ("model", models.CharField(max_length=120)),
                (
                    "model_version",
                    models.CharField(blank=True, default="", max_length=120),
                ),
                ("suite_version", models.CharField(max_length=80)),
                ("domain_lane", models.CharField(max_length=80)),
                ("attempts_per_case", models.PositiveSmallIntegerField(default=5)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("RUNNING", "Running"),
                            ("PASSED", "Passed"),
                            ("FAILED", "Failed"),
                        ],
                        default="RUNNING",
                        max_length=12,
                    ),
                ),
                ("metrics", models.JSONField(default=dict)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="RamaModelCertification",
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
                ("provider", models.CharField(max_length=40)),
                ("model", models.CharField(max_length=120)),
                (
                    "model_version",
                    models.CharField(blank=True, default="", max_length=120),
                ),
                ("domain_lane", models.CharField(max_length=80)),
                ("eval_suite_version", models.CharField(max_length=80)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("CANDIDATE", "Candidate"),
                            ("CERTIFIED", "Certified"),
                            ("REVOKED", "Revoked"),
                        ],
                        default="CANDIDATE",
                        max_length=12,
                    ),
                ),
                ("metrics", models.JSONField(default=dict)),
                ("eval_run_id", models.UUIDField(blank=True, null=True)),
                ("certified_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["provider", "model", "domain_lane", "status"],
                        name="rama_ramamo_provide_eb54e8_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "provider",
                            "model",
                            "model_version",
                            "domain_lane",
                            "eval_suite_version",
                        ),
                        name="rama_unique_model_certification",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="RamaEvidenceTrace",
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
                ("conversation_id", models.UUIDField(db_index=True)),
                ("message_id", models.UUIDField(blank=True, null=True)),
                ("engine_mode", models.CharField(max_length=12)),
                ("answer_mode", models.CharField(max_length=16)),
                ("coverage", models.CharField(max_length=16)),
                ("provider", models.CharField(blank=True, default="", max_length=40)),
                ("model", models.CharField(blank=True, default="", max_length=120)),
                ("intent", models.CharField(blank=True, default="", max_length=120)),
                ("subject", models.JSONField(default=dict)),
                ("evidence_summary", models.JSONField(default=dict)),
                ("violations", models.JSONField(default=list)),
                (
                    "input_hash",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=64
                    ),
                ),
                ("redacted_input", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "landlord",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rama_evidence_traces",
                        to="users.landlordprofile",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["landlord", "engine_mode", "created_at"],
                        name="rama_ramaev_landlor_66dc51_idx",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="RamaEvalCase",
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
                ("name", models.CharField(max_length=180)),
                ("suite_version", models.CharField(max_length=80)),
                (
                    "split",
                    models.CharField(
                        choices=[
                            ("development", "Development"),
                            ("regression", "Regression"),
                            ("holdout", "Hidden holdout"),
                        ],
                        max_length=16,
                    ),
                ),
                ("domain_lane", models.CharField(max_length=80)),
                ("severity", models.CharField(default="critical", max_length=16)),
                ("transcript", models.JSONField(default=list)),
                ("expected", models.JSONField(default=dict)),
                ("prohibited_claims", models.JSONField(default=list)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "source_trace",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="eval_cases",
                        to="rama.ramaevidencetrace",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("name", "suite_version"), name="rama_unique_eval_case"
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="RamaEvalResult",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("attempt", models.PositiveSmallIntegerField()),
                ("passed", models.BooleanField(default=False)),
                ("scores", models.JSONField(default=dict)),
                ("failures", models.JSONField(default=list)),
                ("trace", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "case",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="results",
                        to="rama.ramaevalcase",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="results",
                        to="rama.ramaevalrun",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("run", "case", "attempt"),
                        name="rama_unique_eval_attempt",
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="RamaKnowledgeSource",
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
                            ("OFFICIAL", "Official source"),
                            ("PRIVATE", "Private landlord source"),
                        ],
                        max_length=12,
                    ),
                ),
                ("title", models.CharField(max_length=300)),
                ("authority", models.CharField(blank=True, default="", max_length=200)),
                (
                    "canonical_url",
                    models.URLField(blank=True, default="", max_length=1000),
                ),
                (
                    "jurisdiction",
                    models.CharField(blank=True, default="", max_length=80),
                ),
                ("effective_from", models.DateField(blank=True, null=True)),
                ("effective_to", models.DateField(blank=True, null=True)),
                ("version", models.CharField(blank=True, default="", max_length=100)),
                ("sha256", models.CharField(db_index=True, max_length=64)),
                ("metadata", models.JSONField(default=dict)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "landlord",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rama_knowledge_sources",
                        to="users.landlordprofile",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["kind", "jurisdiction", "active"],
                        name="rama_ramakn_kind_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("kind", "landlord", "sha256"),
                        name="rama_unique_knowledge_source",
                        nulls_distinct=False,
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="RamaKnowledgeChunk",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("ordinal", models.PositiveIntegerField()),
                ("heading", models.CharField(blank=True, default="", max_length=300)),
                ("context", models.TextField(blank=True, default="")),
                ("content", models.TextField()),
                (
                    "citation_label",
                    models.CharField(blank=True, default="", max_length=300),
                ),
                (
                    "search_vector",
                    django.contrib.postgres.search.SearchVectorField(null=True),
                ),
                (
                    "embedding",
                    pgvector.django.VectorField(blank=True, dimensions=1536, null=True),
                ),
                (
                    "embedding_model",
                    models.CharField(blank=True, default="", max_length=120),
                ),
                ("token_count", models.PositiveIntegerField(default=0)),
                ("metadata", models.JSONField(default=dict)),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="chunks",
                        to="rama.ramaknowledgesource",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["source", "ordinal"], name="rama_ramakn_source_idx"
                    ),
                    pgvector.django.HnswIndex(
                        ef_construction=64,
                        fields=["embedding"],
                        m=16,
                        name="rama_chunk_embedding_hnsw",
                        opclasses=["vector_cosine_ops"],
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("source", "ordinal"), name="rama_unique_knowledge_chunk"
                    )
                ],
            },
        ),
    ]
