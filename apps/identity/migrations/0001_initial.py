import uuid
from django.db import migrations, models
import django.contrib.auth.models
import django.utils.timezone

class Migration(migrations.Migration):
    initial = True
    dependencies = [('auth', '0012_alter_user_first_name_max_length')]
    operations = [migrations.CreateModel(
        name='User',
        fields=[
            ('password', models.CharField(max_length=128, verbose_name='password')),
            ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
            ('is_superuser', models.BooleanField(default=False, help_text='Designates that this user has all permissions without explicitly assigning them.', verbose_name='superuser status')),
            ('first_name', models.CharField(blank=True, max_length=150, verbose_name='first name')),
            ('last_name', models.CharField(blank=True, max_length=150, verbose_name='last name')),
            ('is_staff', models.BooleanField(default=False, help_text='Designates whether the user can log into this admin site.', verbose_name='staff status')),
            ('is_active', models.BooleanField(default=True, help_text='Designates whether this user should be treated as active.', verbose_name='active')),
            ('date_joined', models.DateTimeField(default=django.utils.timezone.now, verbose_name='date joined')),
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('supabase_user_id', models.CharField(db_index=True, max_length=255, unique=True)),
            ('username', models.CharField(blank=True, max_length=150, null=True, unique=True)),
            ('email', models.EmailField(blank=True, max_length=254)),
            ('full_name', models.CharField(blank=True, max_length=255)),
            ('display_name', models.CharField(blank=True, max_length=255)),
            ('avatar_url', models.URLField(blank=True)),
            ('job_title', models.CharField(blank=True, max_length=255)),
            ('contact', models.CharField(blank=True, max_length=255)),
            ('country', models.CharField(blank=True, max_length=100)),
            ('timezone', models.CharField(blank=True, max_length=100)),
            ('bio', models.TextField(blank=True)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('groups', models.ManyToManyField(blank=True, help_text='The groups this user belongs to.', related_name='user_set', related_query_name='user', to='auth.group', verbose_name='groups')),
            ('user_permissions', models.ManyToManyField(blank=True, help_text='Specific permissions for this user.', related_name='user_set', related_query_name='user', to='auth.permission', verbose_name='user permissions')),
        ],
        options={'abstract': False},
        managers=[('objects', django.contrib.auth.models.UserManager())],
    )]
