from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0004_organization_slug_organization_timezone"),
        ("assets", "0002_rename_assets_asse_owner_i_f137e6_idx_assets_asse_owner_i_96bc18_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="assets",
                to="identity.organization",
            ),
        ),
        migrations.AddIndex(
            model_name="asset",
            index=models.Index(fields=["organization", "-created_at"], name="assets_asse_organiz_41201f_idx"),
        ),
    ]
