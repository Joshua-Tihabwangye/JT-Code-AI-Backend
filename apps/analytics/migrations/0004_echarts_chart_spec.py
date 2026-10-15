# Interactive chart specs move from Plotly figure JSON to Apache ECharts options. Plotly
# JSON is not convertible, so the inline copy is dropped; retrying a visualization
# regenerates it (and its spec asset) in the new format.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0003_sandbox_profiles_and_chart_specs"),
    ]

    operations = [
        migrations.RemoveField(model_name="visualization", name="plotly_spec"),
        migrations.AddField(
            model_name="visualization",
            name="chart_spec",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
