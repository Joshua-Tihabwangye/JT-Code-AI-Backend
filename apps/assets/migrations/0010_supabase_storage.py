"""Move the asset registry schema to provider-neutral Supabase Storage fields."""

from django.db import migrations, models


def quarantine_pre_supabase_assets(apps, schema_editor):  # noqa: ARG001
    """Do not serve ImageKit-era records as if they existed in the new bucket.

    Byte migration is deliberately an explicit operator action: a database row
    alone cannot copy a private object from a different provider.  The command
    ``migrate_legacy_assets --map`` re-verifies objects after they are imported
    into the private Supabase bucket and returns the rows to READY.
    """
    Asset = apps.get_model("assets", "Asset")
    Asset.objects.exclude(status="deleted").update(
        status="quarantined",
        deletion_error="Asset bytes must be imported into Supabase Storage before this record can be served.",
    )


class Migration(migrations.Migration):
    dependencies = [("assets", "0009_asset_registry_completion")]

    operations = [
        migrations.RenameField(model_name="asset", old_name="imagekit_file_id", new_name="storage_object_id"),
        migrations.AlterField(
            model_name="asset", name="storage_object_id", field=models.CharField(max_length=1000, unique=True)
        ),
        migrations.RenameField(model_name="asset", old_name="imagekit_file_path", new_name="storage_key"),
        migrations.AddField(
            model_name="asset",
            name="storage_bucket",
            field=models.CharField(default="jt-code-assets", max_length=100),
        ),
        migrations.RenameField(model_name="asset", old_name="secure_url", new_name="storage_url"),
        migrations.AlterField(model_name="asset", name="storage_url", field=models.URLField(blank=True, max_length=1000)),
        migrations.RenameField(
            model_name="uploadintent", old_name="imagekit_file_id", new_name="storage_object_key"
        ),
        migrations.AlterField(
            model_name="uploadintent", name="storage_object_key", field=models.CharField(blank=True, max_length=1000)
        ),
        migrations.RunPython(quarantine_pre_supabase_assets, migrations.RunPython.noop),
    ]
