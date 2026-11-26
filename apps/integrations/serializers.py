import re

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


_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def validate_webhook_url(value: str) -> str:
    """A public HTTPS endpoint, checked with the same rules deliveries use (``apps.tools.egress``)."""
    from apps.tools.egress import EgressDenied, EgressError, validate_url

    try:
        validate_url(value)
    except (EgressDenied, EgressError) as exc:
        raise serializers.ValidationError(str(exc)) from exc
    return value


def validate_webhook_events(value: list[str]) -> list[str]:
    from apps.integrations.outbound import WEBHOOK_EVENTS

    unknown = sorted(set(value) - set(WEBHOOK_EVENTS))
    if unknown:
        raise serializers.ValidationError(f"Unknown events: {', '.join(unknown)}.")
    return sorted(set(value))


def validate_webhook_headers(value: dict) -> dict:
    from apps.integrations.outbound import RESERVED_HEADERS, RESERVED_PREFIX

    if not isinstance(value, dict) or len(value) > 10:
        raise serializers.ValidationError("At most 10 headers.")
    for name, header in value.items():
        lowered = str(name).lower()
        if (
            not _HEADER_NAME.match(str(name))
            or lowered in RESERVED_HEADERS
            or lowered.startswith(RESERVED_PREFIX)
        ):
            raise serializers.ValidationError(f"The header {name!r} cannot be set.")
        if not isinstance(header, str) or len(header) > 512 or "\r" in header or "\n" in header:
            raise serializers.ValidationError(
                f"The value of {name!r} must be one line of at most 512 characters."
            )
    return value


class WebhookSerializer(serializers.ModelSerializer):
    """An endpoint as listed: never its signing secret (returned once, at creation or rotation)."""

    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    events = serializers.ListField(child=serializers.CharField(), required=False)
    # Users pause and resume; ``disabled`` (too many failures) and ``error`` are set by the platform.
    status = serializers.ChoiceField(choices=["active", "paused"], required=False)

    class Meta:
        model = Webhook
        fields = [
            "id",
            "name",
            "url",
            "status",
            "event_filter",
            "events",
            "headers",
            "is_verified",
            "last_triggered_at",
            "last_success_at",
            "last_failure_at",
            "failure_count",
            "created_by_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "is_verified",
            "last_triggered_at",
            "last_success_at",
            "last_failure_at",
            "failure_count",
            "created_at",
            "updated_at",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["status"] = instance.status  # also "disabled"/"error"
        return data

    def validate_url(self, value: str) -> str:
        return validate_webhook_url(value)

    def validate_events(self, value: list[str]) -> list[str]:
        return validate_webhook_events(value)

    def validate_headers(self, value: dict) -> dict:
        return validate_webhook_headers(value)

    def validate(self, attrs):
        if "events" in attrs and "event_filter" not in attrs:
            attrs["event_filter"] = Webhook.EventFilter.CUSTOM if attrs["events"] else Webhook.EventFilter.ALL
        if attrs.get("event_filter") == Webhook.EventFilter.CUSTOM and not attrs.get(
            "events", getattr(self.instance, "events", None)
        ):
            raise serializers.ValidationError({"events": "Choose at least one event, or receive all events."})
        if attrs.get("status") == Webhook.Status.ACTIVE and self.instance is not None:
            attrs["failure_count"] = 0  # resuming starts a fresh failure count
        return attrs


class WebhookCreateSerializer(WebhookSerializer):
    class Meta(WebhookSerializer.Meta):
        fields = ["name", "url", "event_filter", "events", "headers"]
        read_only_fields: list[str] = []


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
