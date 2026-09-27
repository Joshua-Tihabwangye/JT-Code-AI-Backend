import uuid

from django.conf import settings
from django.db import models
from pgvector.django import VectorField


class Collection(models.Model):
    class EmbeddingProvider(models.TextChoices):
        OPENAI = "openai", "OpenAI"
        COHERE = "cohere", "Cohere"
        HUGGINGFACE = "huggingface", "HuggingFace"
        VOYAGE = "voyage", "Voyage AI"
        CUSTOM = "custom", "Custom"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="collections"
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    embedding_provider = models.CharField(
        max_length=30, choices=EmbeddingProvider.choices, default=EmbeddingProvider.OPENAI
    )
    embedding_model = models.CharField(max_length=100)
    embedding_dimensions = models.PositiveIntegerField(default=1536)
    chunk_size = models.PositiveIntegerField(default=1000)
    chunk_overlap = models.PositiveIntegerField(default=200)
    metadata = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    document_count = models.PositiveIntegerField(default=0)
    chunk_count = models.PositiveIntegerField(default=0)
    storage_size_bytes = models.PositiveBigIntegerField(default=0)
    last_indexed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="created_collections", null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("organization", "-created_at")),
        ]

    def __str__(self):
        return f"{self.name} ({self.organization})"


class Source(models.Model):
    class SourceType(models.TextChoices):
        FILE = "file", "File Upload"
        URL = "url", "Web URL"
        CONNECTOR = "connector", "External Connector"
        TEXT = "text", "Raw Text"
        DATABASE = "database", "Database"
        EMAIL = "email", "Email"
        DRIVE = "drive", "Google Drive / OneDrive"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        INDEXED = "indexed", "Indexed"
        FAILED = "failed", "Failed"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="sources")
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    name = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    config = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    document_count = models.PositiveIntegerField(default=0)
    chunk_count = models.PositiveIntegerField(default=0)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    sync_schedule = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="created_sources", null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("collection", "-created_at")),
            models.Index(fields=("status",)),
        ]

    def __str__(self):
        return f"{self.name} ({self.source_type})"


class Document(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PARSING = "parsing", "Parsing"
        CHUNKING = "chunking", "Chunking"
        EMBEDDING = "embedding", "Embedding"
        INDEXED = "indexed", "Indexed"
        FAILED = "failed", "Failed"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="documents")
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="documents")
    external_id = models.CharField(max_length=500, blank=True)
    title = models.CharField(max_length=500)
    content_hash = models.CharField(max_length=64, db_index=True)
    mime_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    language = models.CharField(max_length=10, blank=True)
    page_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    metadata = models.JSONField(default=dict, blank=True)
    acl = models.JSONField(default=dict, blank=True)
    classification = models.CharField(max_length=20, default="internal")
    chunk_count = models.PositiveIntegerField(default=0)
    vector_ids = models.JSONField(default=list, blank=True)
    last_error = models.TextField(blank=True)
    indexed_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("collection", "-created_at")),
            models.Index(fields=("source", "-created_at")),
            models.Index(fields=("status",)),
            models.Index(fields=("content_hash",)),
        ]

    def __str__(self):
        return f"{self.title} ({self.status})"


class Chunk(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="chunks")
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="chunks")
    chunk_index = models.PositiveIntegerField()
    content = models.TextField()
    token_count = models.PositiveIntegerField(default=0)
    heading_path = models.JSONField(default=list, blank=True)
    page_number = models.PositiveIntegerField(null=True, blank=True)
    offset_start = models.PositiveIntegerField(default=0)
    offset_end = models.PositiveIntegerField(default=0)
    vector_id = models.CharField(max_length=100, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    acl = models.JSONField(default=dict, blank=True)
    embedding = VectorField(
        dimensions=settings.VECTOR_EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    embedding_model = models.CharField(max_length=100, blank=True)
    embedding_dimensions = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("document", "chunk_index")
        indexes = [
            models.Index(fields=("document", "chunk_index")),
            models.Index(fields=("collection",)),
            models.Index(fields=("vector_id",)),
        ]

    def __str__(self):
        return f"Chunk {self.chunk_index} of {self.document_id}"


class SyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        PARTIAL = "partial", "Partial"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="sync_runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    documents_processed = models.PositiveIntegerField(default=0)
    documents_added = models.PositiveIntegerField(default=0)
    documents_updated = models.PositiveIntegerField(default=0)
    documents_deleted = models.PositiveIntegerField(default=0)
    chunks_created = models.PositiveIntegerField(default=0)
    chunks_updated = models.PositiveIntegerField(default=0)
    chunks_deleted = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-started_at",)

    def __str__(self):
        return f"SyncRun {self.id} - {self.status}"


class Citation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey("jobs.Job", on_delete=models.CASCADE, related_name="citations")
    chunk = models.ForeignKey(Chunk, on_delete=models.CASCADE, related_name="citations")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="citations")
    relevance_score = models.FloatField()
    citation_index = models.PositiveIntegerField()
    snippet = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("citation_index",)
        indexes = [
            models.Index(fields=("job", "citation_index")),
        ]

    def __str__(self):
        return f"Citation {self.citation_index} for Job {self.job_id}"
