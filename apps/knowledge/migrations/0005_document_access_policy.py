import django.db.models.deletion
import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("knowledge", "0004_agentic_rag_completion"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ragevaluation",
            name="created_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="rag_evaluations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="collection",
            name="embedding_provider",
            field=models.CharField(
                choices=[
                    ("openai", "OpenAI"),
                    ("gemini", "Google Gemini"),
                    ("echo", "Echo (development/test only)"),
                ],
                default="openai",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="source",
            name="source_type",
            field=models.CharField(
                choices=[("file", "File Upload"), ("url", "Web URL"), ("text", "Raw Text")],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="visibility",
            field=models.CharField(
                choices=[("organization", "Organization"), ("restricted", "Restricted")],
                db_index=True,
                default="organization",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="DocumentAccessGrant",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_grants",
                        to="knowledge.document",
                    ),
                ),
                (
                    "granted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="granted_knowledge_documents",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="knowledge_document_grants",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="documentaccessgrant",
            constraint=models.UniqueConstraint(
                fields=("document", "user"), name="uniq_document_user_grant"
            ),
        ),
        migrations.AddIndex(
            model_name="documentaccessgrant",
            index=models.Index(fields=["user", "document"], name="knowledge_d_user_id_622e1d_idx"),
        ),
    ]
