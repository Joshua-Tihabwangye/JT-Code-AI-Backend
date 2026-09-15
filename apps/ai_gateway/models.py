import uuid

from django.db import models


class Provider(models.Model):
    class Type(models.TextChoices):
        OPENAI = 'openai', 'OpenAI'
        ANTHROPIC = 'anthropic', 'Anthropic'
        GOOGLE = 'google', 'Google'
        COHERE = 'cohere', 'Cohere'
        MISTRAL = 'mistral', 'Mistral'
        PERPLEXITY = 'perplexity', 'Perplexity'
        TOGETHER = 'together', 'Together AI'
        FIREWORKS = 'fireworks', 'Fireworks AI'
        REPLICATE = 'replicate', 'Replicate'
        HUGGINGFACE = 'huggingface', 'HuggingFace'
        OLLAMA = 'ollama', 'Ollama (Local)'
        CUSTOM = 'custom', 'Custom'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        DEPRECATED = 'deprecated', 'Deprecated'
        DISABLED = 'disabled', 'Disabled'
        MAINTENANCE = 'maintenance', 'Maintenance'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    type = models.CharField(max_length=30, choices=Type.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    base_url = models.URLField(blank=True)
    api_version = models.CharField(max_length=50, blank=True)
    capabilities = models.JSONField(default=list, blank=True)
    supported_modalities = models.JSONField(default=list, blank=True)
    rate_limits = models.JSONField(default=dict, blank=True)
    regions = models.JSONField(default=list, blank=True)
    privacy_tier = models.CharField(max_length=20, default='standard')
    credentials_ref = models.CharField(max_length=255, blank=True)
    default_headers = models.JSONField(default=dict, blank=True)
    timeout_seconds = models.PositiveIntegerField(default=60)
    max_retries = models.PositiveIntegerField(default=3)
    circuit_breaker_threshold = models.PositiveIntegerField(default=5)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return f'{self.name} ({self.type}) - {self.status}'


class Model(models.Model):
    class Modality(models.TextChoices):
        TEXT = 'text', 'Text'
        IMAGE = 'image', 'Image'
        AUDIO = 'audio', 'Audio'
        VIDEO = 'video', 'Video'
        EMBEDDING = 'embedding', 'Embedding'
        MULTIMODAL = 'multimodal', 'Multimodal'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        DEPRECATED = 'deprecated', 'Deprecated'
        DISABLED = 'disabled', 'Disabled'
        BETA = 'beta', 'Beta'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name='models')
    name = models.CharField(max_length=100)
    display_name = models.CharField(max_length=255)
    modality = models.CharField(max_length=20, choices=Modality.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    context_window = models.PositiveIntegerField(default=4096)
    max_output_tokens = models.PositiveIntegerField(default=4096)
    supports_tools = models.BooleanField(default=False)
    supports_streaming = models.BooleanField(default=True)
    supports_json_mode = models.BooleanField(default=False)
    supports_vision = models.BooleanField(default=False)
    input_price_per_token = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    output_price_per_token = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    cached_input_price_per_token = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    image_price_per_unit = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    audio_price_per_second = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    quality_score = models.FloatField(default=0.0)
    latency_p50_ms = models.PositiveIntegerField(default=0)
    latency_p99_ms = models.PositiveIntegerField(default=0)
    regions = models.JSONField(default=list, blank=True)
    retirement_date = models.DateField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('provider', 'name')
        unique_together = ('provider', 'name')

    def __str__(self):
        return f'{self.provider.name} / {self.name} ({self.modality})'


class ModelPolicy(models.Model):
    class RoutingStrategy(models.TextChoices):
        COST_OPTIMIZED = 'cost_optimized', 'Cost Optimized'
        QUALITY_OPTIMIZED = 'quality_optimized', 'Quality Optimized'
        LATENCY_OPTIMIZED = 'latency_optimized', 'Latency Optimized'
        BALANCED = 'balanced', 'Balanced'
        MANUAL = 'manual', 'Manual Selection'

    class FallbackPolicy(models.TextChoices):
        NONE = 'none', 'No Fallback'
        CHEAPER = 'cheaper', 'Cheaper Model'
        FASTER = 'faster', 'Faster Model'
        ANY_AVAILABLE = 'any_available', 'Any Available'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    task_type = models.CharField(max_length=50)
    routing_strategy = models.CharField(
        max_length=30, choices=RoutingStrategy.choices, default=RoutingStrategy.BALANCED
    )
    primary_model = models.ForeignKey(
        Model,
        on_delete=models.PROTECT,
        related_name='primary_policies'
    )
    fallback_models = models.ManyToManyField(Model, related_name='fallback_policies', blank=True)
    fallback_policy = models.CharField(
        max_length=20, choices=FallbackPolicy.choices, default=FallbackPolicy.CHEAPER
    )
    max_cost_usd = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    max_latency_ms = models.PositiveIntegerField(null=True, blank=True)
    min_quality_score = models.FloatField(default=0.0)
    allowed_providers = models.ManyToManyField(Provider, blank=True)
    blocked_providers = models.ManyToManyField(Provider, related_name='blocked_policies', blank=True)
    allowed_regions = models.JSONField(default=list, blank=True)
    required_capabilities = models.JSONField(default=list, blank=True)
    plan_restrictions = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('task_type', 'name')

    def __str__(self):
        return f'{self.name} ({self.task_type}) - {self.routing_strategy}'


class ModelRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'
        TIMEOUT = 'timeout', 'Timeout'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.UUIDField(db_index=True)
    job_id = models.UUIDField(null=True, blank=True, db_index=True)
    job_step_id = models.UUIDField(null=True, blank=True, db_index=True)
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name='model_runs')
    model = models.ForeignKey(Model, on_delete=models.PROTECT, related_name='model_runs')
    policy = models.ForeignKey(
        ModelPolicy, on_delete=models.SET_NULL, related_name='model_runs', null=True, blank=True
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    cached_tokens = models.PositiveBigIntegerField(default=0)
    reasoning_tokens = models.PositiveBigIntegerField(default=0)
    image_count = models.PositiveIntegerField(default=0)
    audio_seconds = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    provider_cost_usd = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    estimated_cost_usd = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    time_to_first_token_ms = models.PositiveIntegerField(null=True, blank=True)
    trace_id = models.CharField(max_length=100, blank=True, db_index=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    fallback_used = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('request_id',)),
            models.Index(fields=('job_id',)),
            models.Index(fields=('provider', 'model', '-created_at')),
            models.Index(fields=('trace_id',)),
        ]

    def __str__(self):
        return f'{self.provider.name}/{self.model.name} - {self.status}'


class Prompt(models.Model):
    class Category(models.TextChoices):
        SYSTEM = 'system', 'System Prompt'
        TASK = 'task', 'Task Prompt'
        TEMPLATE = 'template', 'Template'
        CHAIN_OF_THOUGHT = 'chain_of_thought', 'Chain of Thought'
        FEW_SHOT = 'few_shot', 'Few-shot Examples'
        GUARDRAIL = 'guardrail', 'Guardrail'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    category = models.CharField(max_length=30, choices=Category.choices)
    content = models.TextField()
    variables = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True)
    model_constraints = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    tags = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        'identity.User',
        on_delete=models.SET_NULL,
        related_name='created_prompts',
        null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('category', 'name')

    def __str__(self):
        return f'{self.name} ({self.category}) v{self.version}'


class Evaluation(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    class Type(models.TextChoices):
        ACCURACY = 'accuracy', 'Accuracy'
        FAITHFULNESS = 'faithfulness', 'Faithfulness'
        HALLUCINATION = 'hallucination', 'Hallucination'
        TOXICITY = 'toxicity', 'Toxicity'
        BIAS = 'bias', 'Bias'
        LATENCY = 'latency', 'Latency'
        COST = 'cost', 'Cost'
        CUSTOM = 'custom', 'Custom'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    type = models.CharField(max_length=20, choices=Type.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    model = models.ForeignKey(Model, on_delete=models.CASCADE, related_name='evaluations')
    prompt = models.ForeignKey(Prompt, on_delete=models.CASCADE, related_name='evaluations')
    dataset_name = models.CharField(max_length=255)
    dataset_version = models.CharField(max_length=50)
    metrics = models.JSONField(default=dict, blank=True)
    threshold_passed = models.BooleanField(null=True, blank=True)
    results_summary = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    run_id = models.CharField(max_length=100, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        'identity.User',
        on_delete=models.SET_NULL,
        related_name='created_evaluations',
        null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.name} ({self.type}) - {self.status}'