from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0003_asset_organization"),
    ]

    operations = [
        migrations.RenameField(
            model_name="asset",
            old_name="cloudinary_public_id",
            new_name="imagekit_file_id",
        ),
        migrations.AddField(
            model_name="asset",
            name="imagekit_file_path",
            field=models.CharField(blank=True, max_length=1000),
        ),
    ]
