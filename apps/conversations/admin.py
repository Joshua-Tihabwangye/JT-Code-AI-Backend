from django.contrib import admin
from apps.conversations.models import Conversation, Message, ChatRequest


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('id', 'owner', 'title', 'created_at', 'updated_at')
    list_filter = ('created_at', 'updated_at')
    search_fields = ('owner__email', 'title')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'conversation', 'role', 'created_at')
    list_filter = ('role', 'created_at')
    search_fields = ('conversation__title', 'content')
    readonly_fields = ('id', 'created_at')
    ordering = ('-created_at',)


@admin.register(ChatRequest)
class ChatRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'owner', 'conversation', 'task_type', 'status', 'created_at')
    list_filter = ('status', 'task_type', 'created_at')
    search_fields = ('owner__email', 'idempotency_key', 'trace_id')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)