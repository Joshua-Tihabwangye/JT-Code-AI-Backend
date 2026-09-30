from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_gateway", "0006_prompt_evaluation_organization")]

    operations = [
        migrations.AlterField(
            model_name="prompt", name="slug", field=models.SlugField(),
        ),
        migrations.AlterField(
            model_name="evaluation", name="slug", field=models.SlugField(),
        ),
        migrations.AddConstraint(
            model_name="prompt",
            constraint=models.UniqueConstraint(
                fields=("organization", "slug"), name="uniq_prompt_org_slug"
            ),
        ),
        migrations.AddConstraint(
            model_name="evaluation",
            constraint=models.UniqueConstraint(
                fields=("organization", "slug"), name="uniq_evaluation_org_slug"
            ),
        ),
    ]
