import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='Conversation', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('title', models.CharField(default='New conversation', max_length=255)),
            ('created_at', models.DateTimeField(auto_now_add=True)), ('updated_at', models.DateTimeField(auto_now=True)),
            ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='conversations', to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.CreateModel(name='Message', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('role', models.CharField(choices=[('system','System'),('user','User'),('assistant','Assistant'),('tool','Tool')], max_length=16)),
            ('content', models.TextField()), ('metadata', models.JSONField(blank=True, default=dict)), ('created_at', models.DateTimeField(auto_now_add=True)),
            ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='conversations.conversation')),
        ]),
        migrations.CreateModel(name='ChatRequest', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('idempotency_key', models.CharField(max_length=255)), ('task_type', models.CharField(default='GENERAL_QUESTION', max_length=64)),
            ('input_text', models.TextField()), ('output_text', models.TextField(blank=True)),
            ('status', models.CharField(choices=[('queued','Queued'),('running','Running'),('completed','Completed'),('failed','Failed'),('cancelled','Cancelled')], default='queued', max_length=16)),
            ('error_code', models.CharField(blank=True, max_length=100)), ('trace_id', models.CharField(db_index=True, max_length=100)),
            ('locale', models.CharField(blank=True, max_length=32)), ('timezone', models.CharField(blank=True, max_length=100)),
            ('created_at', models.DateTimeField(auto_now_add=True)), ('updated_at', models.DateTimeField(auto_now=True)),
            ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='requests', to='conversations.conversation')),
            ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='chat_requests', to=settings.AUTH_USER_MODEL)),
        ], options={'indexes': [models.Index(fields=['owner','-created_at'], name='conversatio_owner_i_26f038_idx'), models.Index(fields=['status','-created_at'], name='conversatio_status_836802_idx')], 'constraints': [models.UniqueConstraint(fields=('owner','idempotency_key'), name='uniq_chat_idempotency')]})
    ]
