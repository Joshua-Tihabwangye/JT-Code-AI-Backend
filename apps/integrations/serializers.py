from rest_framework import serializers

from apps.integrations.models import (
    APIKey,
    Connector,
    ConnectorAccount,
    KafkaConsumer,
    Webhook,
    WebhookDelivery,
)


class ConnectorSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    auth_type_display = serializers.CharField(source="get_auth_type_display", read_only=True)

    class Meta:
        model = Connector
        fields = [
            "id",
            "slug",
            "name",
            "description",
            "category",
            "category_display",
            "auth_type",
            "auth_type_display",
            "config_schema",
            "required_scopes",
            "icon_url",
            "documentation_url",
            "is_active",
            "is_verified",
            "supported_operations",
            "rate_limits",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ConnectorAccountSerializer(serializers.ModelSerializer):
    connector_name = serializers.CharField(source="connector.name", read_only=True)
    connector_slug = serializers.CharField(source="connector.slug", read_only=True)
    connector_category = serializers.CharField(source="connector.category", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = ConnectorAccount
        fields = [
            "id",
            "organization",
            "organization_name",
            "connector",
            "connector_name",
            "connector_slug",
            "connector_category",
            "user",
            "user_email",
            "name",
            "status",
            "status_display",
            "scopes_granted",
            "token_expires_at",
            "last_sync_at",
            "last_error",
            "is_default",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "organization",
            "user",
            "connector",
            "id",
            "credentials_ref",
            "encrypted_config",
            "token_expires_at",
            "last_sync_at",
            "last_error",
            "created_at",
            "updated_at",
        ]


class ConnectorAccountCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConnectorAccount
        fields = ["connector", "name"]


class ConnectorAccountAuthSerializer(serializers.Serializer):
    authorization_code = serializers.CharField(required=False)
    redirect_uri = serializers.URLField(required=False)
    credentials = serializers.JSONField(required=False)


class WebhookSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    event_filter_display = serializers.CharField(source="get_event_filter_display", read_only=True)

    class Meta:
        model = Webhook
        fields = [
            "id",
            "organization",
            "organization_name",
            "name",
            "url",
            "status",
            "status_display",
            "event_filter",
            "event_filter_display",
            "events",
            "headers",
            "retry_policy",
            "is_verified",
            "last_triggered_at",
            "last_success_at",
            "last_failure_at",
            "failure_count",
            "metadata",
            "created_by",
            "created_by_email",
            "created_at",
            "updated_at",
        ]
        # The signing secret is never listed: it is returned once, in the create response.
        read_only_fields = [
            "organization",
            "created_by",
            "id",
            "is_verified",
            "last_triggered_at",
            "last_success_at",
            "last_failure_at",
            "failure_count",
            "created_at",
            "updated_at",
        ]


class WebhookCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Webhook
        fields = ["name", "url", "events", "headers", "retry_policy"]


class WebhookDeliverySerializer(serializers.ModelSerializer):
    webhook_name = serializers.CharField(source="webhook.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = WebhookDelivery
        fields = [
            "id",
            "webhook",
            "webhook_name",
            "event_type",
            "payload",
            "status",
            "status_display",
            "attempt",
            "max_attempts",
            "response_status",
            "response_body",
            "response_headers",
            "error_message",
            "next_retry_at",
            "delivered_at",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class APIKeySerializer(serializers.ModelSerializer):
    """A key as listed: never the key itself or its digest. Only ``name`` can change afterwards."""

    user_email = serializers.EmailField(source="user.email", read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = APIKey
        fields = [
            "id",
            "organization",
            "organization_name",
            "user",
            "user_email",
            "name",
            "prefix",
            "status",
            "status_display",
            "scopes",
            "rate_limit",
            "last_used_at",
            "expires_at",
            "created_at",
            "updated_at",
        ]
        # Status changes only through revoke; scopes, limit and expiry need a new key.
        read_only_fields = [f for f in fields if f != "name"]


class APIKeyCreateSerializer(serializers.ModelSerializer):
    # Omitted: read-only (the least privilege). An explicit empty list is an error.
    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=["read", "write"]), allow_empty=False, default=lambda: ["read"]
    )
    rate_limit = serializers.IntegerField(min_value=1, required=False)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    class Meta:
        model = APIKey
        fields = ["name", "scopes", "rate_limit", "expires_at"]

    def validate_scopes(self, value: list[str]) -> list[str]:
        return sorted(set(value))

    def validate_rate_limit(self, value: int) -> int:
        from django.conf import settings

        if value > settings.API_KEY_MAX_RATE_LIMIT:
            raise serializers.ValidationError(f"At most {settings.API_KEY_MAX_RATE_LIMIT} requests per hour.")
        return value

    def validate_expires_at(self, value):
        from django.utils import timezone

        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("The expiry must be in the future.")
        return value


class KafkaConsumerSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = KafkaConsumer
        fields = [
            "id",
            "organization",
            "organization_name",
            "name",
            "group_id",
            "topics",
            "status",
            "status_display",
            "config",
            "assigned_partitions",
            "lag",
            "last_poll_at",
            "last_error",
            "metadata",
            "created_by",
            "created_by_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "organization",
            "created_by",
            "id",
            "assigned_partitions",
            "lag",
            "last_poll_at",
            "last_error",
            "created_at",
            "updated_at",
        ]


class KafkaConsumerCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = KafkaConsumer
        fields = ["name", "group_id", "topics", "config"]
