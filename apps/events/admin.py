from django.contrib import admin

from apps.events.models import ConsumedEvent, DeadLetterEvent, OutboxEvent


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ("id", "topic", "event_key", "status", "attempts", "created_at", "published_at")
    list_filter = ("status", "topic", "created_at")
    search_fields = ("topic", "event_key")
    readonly_fields = ("id", "created_at", "published_at")
    ordering = ("-created_at",)


@admin.register(ConsumedEvent)
class ConsumedEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_id",
        "event_type",
        "consumer_group",
        "topic",
        "partition",
        "offset",
        "processed_at",
    )
    list_filter = ("consumer_group", "event_type", "topic")
    search_fields = ("event_id", "event_type", "consumer_group", "topic")
    readonly_fields = ("id", "processed_at")
    ordering = ("-processed_at",)


@admin.register(DeadLetterEvent)
class DeadLetterEventAdmin(admin.ModelAdmin):
    list_display = ("id", "event_type", "consumer_group", "topic", "partition", "offset", "created_at")
    list_filter = ("consumer_group", "event_type", "topic", "created_at")
    search_fields = ("event_id", "event_type", "consumer_group", "topic", "error")
    readonly_fields = ("id", "created_at", "replayed_at")
    ordering = ("-created_at",)
