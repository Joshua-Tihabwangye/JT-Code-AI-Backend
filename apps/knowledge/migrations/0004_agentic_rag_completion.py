import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("knowledge", "0003_add_pgvector_embeddings"),
        ("identity", "0006_seed_default_roles"),
        ("jobs", "0006_callback_delivery_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="chunk",
            name="embedding_version",
            field=models.CharField(blank=True, db_index=True, max_length=160),
        ),
        migrations.CreateModel(
            name="RAGEvaluation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("query", models.TextField()),
                ("expected_chunk_ids", models.JSONField(blank=True, default=list)),
                ("retrieved_chunk_ids", models.JSONField(blank=True, default=list)),
                ("metrics", models.JSONField(blank=True, default=dict)),
                ("passed", models.BooleanField(default=False)),
                ("evaluator", models.CharField(default="heuristic-rag-v1", max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "job",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rag_evaluation",
                        to="jobs.job",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rag_evaluations",
                        to="identity.organization",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddIndex(
            model_name="ragevaluation",
            index=models.Index(fields=("organization", "-created_at"), name="knowledge_r_organiz_014000_idx"),
        ),
    ]
