import uuid
from django.db import migrations, models

class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [migrations.CreateModel(name='OutboxEvent', fields=[
        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ('topic', models.CharField(max_length=255)), ('event_key', models.CharField(max_length=255)), ('payload', models.JSONField()),
        ('headers', models.JSONField(blank=True, default=dict)),
        ('status', models.CharField(choices=[('pending','Pending'),('published','Published'),('failed','Failed')], default='pending', max_length=16)),
        ('attempts', models.PositiveIntegerField(default=0)), ('available_at', models.DateTimeField(auto_now_add=True)),
        ('created_at', models.DateTimeField(auto_now_add=True)), ('published_at', models.DateTimeField(blank=True, null=True)), ('last_error', models.TextField(blank=True)),
    ], options={'indexes': [models.Index(fields=['status','available_at','created_at'], name='events_outb_status_c18a9f_idx')]})]
