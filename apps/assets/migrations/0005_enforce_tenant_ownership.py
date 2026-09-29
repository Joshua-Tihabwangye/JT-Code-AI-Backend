import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0004_imagekit_asset_identity"),
        ("governance", "0007_drop_sqlite_analytics_views"),
    ]

    operations = [
        migrations.AlterField(
            model_name="asset",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="assets",
                to="identity.organization",
            ),
        ),
    ]
