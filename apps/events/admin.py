from django.contrib import admin
from apps.events.models import OutboxEvent


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ('id', 'topic', 'event_key', 'status', 'attempts', 'created_at', 'published_at')
    list_filter = ('status', 'topic', 'created_at')
    search_fields = ('topic', 'event_key')
    readonly_fields = ('id', 'created_at', 'published_at')
    ordering = ('-created_at',)