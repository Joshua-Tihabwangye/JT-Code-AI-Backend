import django.db.models.deletion
import uuid

from django.conf import settings
from django.db import migrations, models


def quarantine_unmapped_legacy_rows(apps, schema_editor):
    Asset = apps.get_model("assets", "Asset")
    Asset.objects.filter(imagekit_file_path="").update(
        status="quarantined",
        provenance={"provider": "legacy", "migration_required": True},
        deletion_error="Legacy provider identity requires an explicit ImageKit mapping.",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0006_asset_lifecycle_and_provenance"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="provider_fingerprint",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.CreateModel(
            name="UploadIntent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("token", models.CharField(max_length=100, unique=True)),
                ("folder", models.CharField(max_length=1000)),
                ("file_name", models.CharField(max_length=500)),
                ("original_filename", models.CharField(max_length=500)),
                ("content_type", models.CharField(max_length=255)),
                ("expected_bytes", models.PositiveBigIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("completed", "Completed"),
                            ("expired", "Expired"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("imagekit_file_id", models.CharField(blank=True, max_length=500)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="asset_upload_intents",
                        to="identity.organization",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="asset_upload_intents",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="uploadintent",
            index=models.Index(
                fields=["owner", "status", "expires_at"],
                name="assets_upl_owner_i_21004c_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="uploadintent",
            index=models.Index(
                fields=["organization", "status"], name="assets_upl_organiz_3a50a0_idx"
            ),
        ),
        migrations.RunPython(quarantine_unmapped_legacy_rows, migrations.RunPython.noop),
    ]
