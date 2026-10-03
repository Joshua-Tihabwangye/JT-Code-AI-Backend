import django.db.models.deletion

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assets", "0007_upload_intents_and_integrity"), ("documents", "0002_enforce_tenant_ownership")]

    operations = [
        migrations.AddField(
            model_name="document",
            name="rendered_asset",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="rendered_documents",
                to="assets.asset",
            ),
        )
    ]
