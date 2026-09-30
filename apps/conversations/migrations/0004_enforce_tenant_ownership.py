import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0003_chatrequest_organization_conversation_organization_and_more"),
        ("governance", "0007_drop_sqlite_analytics_views"),
    ]

    operations = [
        migrations.AlterField(
            model_name="conversation",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="conversations",
                to="identity.organization",
            ),
        ),
        migrations.AlterField(
            model_name="message",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="messages",
                to="identity.organization",
            ),
        ),
        migrations.AlterField(
            model_name="chatrequest",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="chat_requests",
                to="identity.organization",
            ),
        ),
    ]
