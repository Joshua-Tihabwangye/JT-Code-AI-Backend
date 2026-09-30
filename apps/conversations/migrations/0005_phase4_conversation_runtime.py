import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("conversations", "0004_enforce_tenant_ownership")]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="request_fingerprint",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        migrations.RemoveConstraint(
            model_name="chatrequest",
            name="uniq_chat_idempotency",
        ),
        migrations.AddConstraint(
            model_name="chatrequest",
            constraint=models.UniqueConstraint(
                fields=("organization", "owner", "idempotency_key"),
                name="uniq_chat_org_idempotency",
            ),
        ),
        migrations.CreateModel(
            name="ConversationFeedback",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "rating",
                    models.PositiveSmallIntegerField(
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(5),
                        ]
                    ),
                ),
                ("comment", models.TextField(blank=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "chat_request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="feedback",
                        to="conversations.chatrequest",
                    ),
                ),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feedback",
                        to="conversations.conversation",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversation_feedback",
                        to="identity.organization",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversation_feedback",
                        to="identity.user",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="conversationfeedback",
            constraint=models.UniqueConstraint(
                fields=("owner", "chat_request"),
                name="uniq_feedback_per_owner_chat_request",
            ),
        ),
        migrations.AddIndex(
            model_name="conversationfeedback",
            index=models.Index(
                fields=["conversation", "-created_at"], name="conversatio_convers_8994de_idx"
            ),
        ),
    ]
