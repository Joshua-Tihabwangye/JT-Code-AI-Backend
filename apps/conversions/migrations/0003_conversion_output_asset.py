import django.db.models.deletion

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assets", "0007_upload_intents_and_integrity"), ("conversions", "0002_enforce_tenant_ownership")]

    operations = [
        migrations.AddField(
            model_name="conversionjob",
            name="output_asset",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="conversion_outputs",
                to="assets.asset",
            ),
        )
    ]
