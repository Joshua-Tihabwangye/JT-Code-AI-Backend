import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name='Asset', fields=[
        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ('cloudinary_public_id', models.CharField(max_length=500, unique=True)), ('secure_url', models.URLField(max_length=1000)),
        ('resource_type', models.CharField(max_length=50)), ('format', models.CharField(blank=True, max_length=50)),
        ('bytes', models.PositiveBigIntegerField(default=0)), ('version', models.PositiveBigIntegerField(default=0)),
        ('original_filename', models.CharField(max_length=500)),
        ('status', models.CharField(choices=[('ready','Ready'),('quarantined','Quarantined'),('deleted','Deleted')], default='ready', max_length=20)),
        ('metadata', models.JSONField(blank=True, default=dict)), ('created_at', models.DateTimeField(auto_now_add=True)), ('updated_at', models.DateTimeField(auto_now=True)),
        ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='assets', to=settings.AUTH_USER_MODEL)),
    ], options={'indexes': [models.Index(fields=['owner','-created_at'], name='assets_asse_owner_i_f137e6_idx')]})]
