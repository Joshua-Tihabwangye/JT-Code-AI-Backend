# Plotly figure snapshots are replaced by Apache ECharts options. Old snapshots are
# not convertible, so they are dropped; dataset charts re-render via "refresh".

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("visualize", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(model_name="savedchart", name="figure"),
        migrations.AddField(
            model_name="savedchart",
            name="option",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="savedchart",
            name="explanation",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
