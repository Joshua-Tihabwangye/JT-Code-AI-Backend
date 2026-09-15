from django.contrib import admin
from apps.integrations.models import Connector, ConnectorAccount, Webhook, WebhookDelivery, APIKey, KafkaConsumer


@admin.register(Connector)
class ConnectorAdmin(admin.ModelAdmin):
    list_display = ('slug', 'name', 'category', 'auth_type', 'is_active', 'is_verified', 'created_at')
    list_filter = ('category', 'auth_type', 'is_active', 'is_verified')
    search_fields = ('slug', 'name', 'description')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('category', 'name')


@admin.register(ConnectorAccount)
class ConnectorAccountAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'organization', 'connector', 'user', 'status', 'is_default', 'last_sync_at', 'created_at')
    list_filter = ('status', 'connector', 'is_default', 'created_at')
    search_fields = ('name', 'organization__name', 'user__email')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'connector', 'user')


@admin.register(Webhook)
class WebhookAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'organization', 'url', 'status', 'event_filter', 'is_verified', 'last_triggered_at', 'failure_count', 'created_at')
    list_filter = ('status', 'event_filter', 'is_verified', 'created_at')
    search_fields = ('name', 'organization__name', 'url')
    readonly_fields = ('id', 'secret', 'last_triggered_at', 'last_success_at', 'last_failure_at', 'failure_count', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'created_by')


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ('id', 'webhook', 'event_type', 'status', 'attempt', 'response_status', 'delivered_at', 'created_at')
    list_filter = ('status', 'event_type', 'created_at')
    search_fields = ('webhook__name', 'event_type')
    readonly_fields = ('id', 'created_at')
    ordering = ('-created_at',)
    raw_id_fields = ('webhook',)


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'organization', 'user', 'prefix', 'status', 'rate_limit', 'last_used_at', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('name', 'organization__name', 'user__email', 'prefix')
    readonly_fields = ('id', 'key_hash', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'user')


@admin.register(KafkaConsumer)
class KafkaConsumerAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'organization', 'group_id', 'status', 'lag', 'last_poll_at', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('name', 'organization__name', 'group_id')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'created_by')