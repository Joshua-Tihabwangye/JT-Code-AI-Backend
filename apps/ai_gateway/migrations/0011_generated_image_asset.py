import django.db.models.deletion

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assets", "0007_upload_intents_and_integrity"), ("ai_gateway", "0010_seed_llama_and_model_aliases")]

    operations = [
        migrations.AddField(
            model_name="generatedimage",
            name="asset",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="generated_image",
                to="assets.asset",
            ),
        )
    ]
