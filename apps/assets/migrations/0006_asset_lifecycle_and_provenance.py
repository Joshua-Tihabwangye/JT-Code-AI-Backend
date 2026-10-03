from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assets", "0005_enforce_tenant_ownership")]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="checksum_sha256",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(model_name="asset", name="provenance", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(
            model_name="asset", name="deleted_at", field=models.DateTimeField(blank=True, null=True)
        ),
        migrations.AddField(
            model_name="asset", name="provider_deleted_at", field=models.DateTimeField(blank=True, null=True)
        ),
        migrations.AddField(model_name="asset", name="deletion_error", field=models.TextField(blank=True)),
        migrations.AddField(model_name="asset", name="deletion_attempts", field=models.PositiveIntegerField(default=0)),
    ]
