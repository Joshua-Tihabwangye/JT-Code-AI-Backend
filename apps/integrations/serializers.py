from rest_framework import serializers
from apps.integrations.models import Connector, ConnectorAccount, Webhook, WebhookDelivery, APIKey, KafkaConsumer


class ConnectorSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    auth_type_display = serializers.CharField(source='get_auth_type_display', read_only=True)

    class Meta:
        model = Connector
        fields = [
            'id', 'slug', 'name', 'description', 'category', 'category_display',
            'auth_type', 'auth_type_display', 'config_schema', 'required_scopes',
            'icon_url', 'documentation_url', 'is_active', 'is_verified',
            'supported_operations', 'rate_limits', 'metadata',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ConnectorAccountSerializer(serializers.ModelSerializer):
    connector_name = serializers.CharField(source='connector.name', read_only=True)
    connector_slug = serializers.CharField(source='connector.slug', read_only=True)
    connector_category = serializers.CharField(source='connector.category', read_only=True)
    user_email = serializers.EmailField(source='user.email', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = ConnectorAccount
        fields = [
            'id', 'organization', 'organization_name', 'connector', 'connector_name',
            'connector_slug', 'connector_category', 'user', 'user_email', 'name',
            'status', 'status_display', 'scopes_granted', 'token_expires_at',
            'last_sync_at', 'last_error', 'is_default', 'metadata',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'credentials_ref', 'encrypted_config', 'token_expires_at',
            'last_sync_at', 'last_error', 'created_at', 'updated_at'
        ]


class ConnectorAccountCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConnectorAccount
        fields = ['connector', 'name']


class ConnectorAccountAuthSerializer(serializers.Serializer):
    authorization_code = serializers.CharField(required=False)
    redirect_uri = serializers.URLField(required=False)
    credentials = serializers.JSONField(required=False)


class WebhookSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    event_filter_display = serializers.CharField(source='get_event_filter_display', read_only=True)

    class Meta:
        model = Webhook
        fields = [
            'id', 'organization', 'organization_name', 'name', 'url', 'secret',
            'status', 'status_display', 'event_filter', 'event_filter_display',
            'events', 'headers', 'retry_policy', 'is_verified',
            'last_triggered_at', 'last_success_at', 'last_failure_at',
            'failure_count', 'metadata', 'created_by', 'created_by_email',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'secret', 'is_verified', 'last_triggered_at',
            'last_success_at', 'last_failure_at', 'failure_count',
            'created_at', 'updated_at'
        ]


class WebhookCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Webhook
        fields = ['name', 'url', 'events', 'headers', 'retry_policy']


class WebhookDeliverySerializer(serializers.ModelSerializer):
    webhook_name = serializers.CharField(source='webhook.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = WebhookDelivery
        fields = [
            'id', 'webhook', 'webhook_name', 'event_type', 'payload',
            'status', 'status_display', 'attempt', 'max_attempts',
            'response_status', 'response_body', 'response_headers',
            'error_message', 'next_retry_at', 'delivered_at', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class APIKeySerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    full_key = serializers.SerializerMethodField()

    class Meta:
        model = APIKey
        fields = [
            'id', 'organization', 'organization_name', 'user', 'user_email',
            'name', 'prefix', 'status', 'status_display', 'scopes',
            'rate_limit', 'last_used_at', 'expires_at', 'metadata',
            'full_key', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'prefix', 'key_hash', 'last_used_at',
            'created_at', 'updated_at'
        ]

    def get_full_key(self, obj):
        # Only return full key on creation
        request = self.context.get('request')
        if request and request.method == 'POST':
            return obj.key_hash  # This would be the actual key in a real implementation
        return None


class APIKeyCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = APIKey
        fields = ['name', 'scopes', 'rate_limit', 'expires_at']


class KafkaConsumerSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = KafkaConsumer
        fields = [
            'id', 'organization', 'organization_name', 'name', 'group_id',
            'topics', 'status', 'status_display', 'config', 'assigned_partitions',
            'lag', 'last_poll_at', 'last_error', 'metadata',
            'created_by', 'created_by_email', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'assigned_partitions', 'lag', 'last_poll_at',
            'last_error', 'created_at', 'updated_at'
        ]


class KafkaConsumerCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = KafkaConsumer
        fields = ['name', 'group_id', 'topics', 'config']