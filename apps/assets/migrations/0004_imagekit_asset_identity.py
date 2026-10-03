from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0003_asset_organization"),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="imagekit_file_path",
            field=models.CharField(blank=True, max_length=1000),
        ),
    ]
