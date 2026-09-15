# Generated migration for knowledge app

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('identity', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Collection',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True)),
                ('embedding_provider', models.CharField(choices=[('openai', 'OpenAI'), ('cohere', 'Cohere'), ('huggingface', 'HuggingFace'), ('voyage', 'Voyage AI'), ('custom', 'Custom')], default='openai', max_length=30)),
                ('embedding_model', models.CharField(max_length=100)),
                ('embedding_dimensions', models.PositiveIntegerField(default=1536)),
                ('chunk_size', models.PositiveIntegerField(default=1000)),
                ('chunk_overlap', models.PositiveIntegerField(default=200)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('is_active', models.BooleanField(default=True)),
                ('document_count', models.PositiveIntegerField(default=0)),
                ('chunk_count', models.PositiveIntegerField(default=0)),
                ('storage_size_bytes', models.PositiveBigIntegerField(default=0)),
                ('last_indexed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_collections', to='identity.user')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='collections', to='identity.organization')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='Source',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('source_type', models.CharField(choices=[('file', 'File Upload'), ('url', 'Web URL'), ('connector', 'External Connector'), ('text', 'Raw Text'), ('database', 'Database'), ('email', 'Email'), ('drive', 'Google Drive / OneDrive')], max_length=20)),
                ('name', models.CharField(max_length=500)),
                ('description', models.TextField(blank=True)),
                ('config', models.JSONField(blank=True, default=dict)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('processing', 'Processing'), ('indexed', 'Indexed'), ('failed', 'Failed'), ('deleted', 'Deleted')], default='pending', max_length=20)),
                ('document_count', models.PositiveIntegerField(default=0)),
                ('chunk_count', models.PositiveIntegerField(default=0)),
                ('last_synced_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True)),
                ('sync_schedule', models.CharField(blank=True, max_length=100)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('collection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sources', to='knowledge.collection')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_sources', to='identity.user')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='Document',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('external_id', models.CharField(blank=True, max_length=500)),
                ('title', models.CharField(max_length=500)),
                ('content_hash', models.CharField(db_index=True, max_length=64)),
                ('mime_type', models.CharField(blank=True, max_length=100)),
                ('size_bytes', models.PositiveBigIntegerField(default=0)),
                ('language', models.CharField(blank=True, max_length=10)),
                ('page_count', models.PositiveIntegerField(default=0)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('parsing', 'Parsing'), ('chunking', 'Chunking'), ('embedding', 'Embedding'), ('indexed', 'Indexed'), ('failed', 'Failed'), ('deleted', 'Deleted')], default='pending', max_length=20)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('acl', models.JSONField(blank=True, default=dict)),
                ('classification', models.CharField(default='internal', max_length=20)),
                ('chunk_count', models.PositiveIntegerField(default=0)),
                ('vector_ids', models.JSONField(blank=True, default=list)),
                ('last_error', models.TextField(blank=True)),
                ('indexed_at', models.DateTimeField(blank=True, null=True)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('collection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='documents', to='knowledge.collection')),
                ('source', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='documents', to='knowledge.source')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='Chunk',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('chunk_index', models.PositiveIntegerField()),
                ('content', models.TextField()),
                ('token_count', models.PositiveIntegerField(default=0)),
                ('heading_path', models.JSONField(blank=True, default=list)),
                ('page_number', models.PositiveIntegerField(blank=True, null=True)),
                ('offset_start', models.PositiveIntegerField(default=0)),
                ('offset_end', models.PositiveIntegerField(default=0)),
                ('vector_id', models.CharField(blank=True, max_length=100)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('acl', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('collection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='chunks', to='knowledge.collection')),
                ('document', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='chunks', to='knowledge.document')),
            ],
            options={
                'ordering': ('document', 'chunk_index'),
            },
        ),
        migrations.CreateModel(
            name='SyncRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed'), ('partial', 'Partial')], default='running', max_length=20)),
                ('documents_processed', models.PositiveIntegerField(default=0)),
                ('documents_added', models.PositiveIntegerField(default=0)),
                ('documents_updated', models.PositiveIntegerField(default=0)),
                ('documents_deleted', models.PositiveIntegerField(default=0)),
                ('chunks_created', models.PositiveIntegerField(default=0)),
                ('chunks_updated', models.PositiveIntegerField(default=0)),
                ('chunks_deleted', models.PositiveIntegerField(default=0)),
                ('error_message', models.TextField(blank=True)),
                ('started_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('source', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sync_runs', to='knowledge.source')),
            ],
            options={
                'ordering': ('-started_at',),
            },
        ),
        migrations.CreateModel(
            name='Citation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('relevance_score', models.FloatField()),
                ('citation_index', models.PositiveIntegerField()),
                ('snippet', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('chunk', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='citations', to='knowledge.chunk')),
                ('document', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='citations', to='knowledge.document')),
                ('job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='citations', to='jobs.job')),
            ],
            options={
                'ordering': ('citation_index',),
            },
        ),
        migrations.AddIndex(
            model_name='collection',
            index=models.Index(fields=('organization', '-created_at'), name='knowledge_col_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='source',
            index=models.Index(fields=('collection', '-created_at'), name='knowledge_sou_collect_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='source',
            index=models.Index(fields=('status',), name='knowledge_sou_status_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='document',
            index=models.Index(fields=('collection', '-created_at'), name='knowledge_doc_collect_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='document',
            index=models.Index(fields=('source', '-created_at'), name='knowledge_doc_source__abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='document',
            index=models.Index(fields=('status',), name='knowledge_doc_status_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='document',
            index=models.Index(fields=('content_hash',), name='knowledge_doc_content_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='chunk',
            index=models.Index(fields=('document', 'chunk_index'), name='knowledge_chu_documen_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='chunk',
            index=models.Index(fields=('collection',), name='knowledge_chu_collect_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='chunk',
            index=models.Index(fields=('vector_id',), name='knowledge_chu_vector_abc123_idx'),
        ),
    ]