from rest_framework import serializers

from apps.conversations.models import ChatRequest, Conversation, ConversationFeedback, Message


class ConversationSerializer(serializers.ModelSerializer):
    """The frontend ``Conversation`` shape (camelCase) plus ``archivedAt``."""

    archivedAt = serializers.DateTimeField(source="archived_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)
    archived = serializers.BooleanField(required=False)
    preview = serializers.SerializerMethodField()
    messageCount = serializers.SerializerMethodField()
    hasAttachments = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = (
            "id",
            "title",
            "preview",
            "messageCount",
            "model",
            "pinned",
            "archived",
            "hasAttachments",
            "archivedAt",
            "createdAt",
            "updatedAt",
        )
        read_only_fields = ("id", "archivedAt", "createdAt", "updatedAt")

    def to_representation(self, instance: Conversation) -> dict:
        data = super().to_representation(instance)
        data["archived"] = instance.archived_at is not None
        return data

    def get_preview(self, obj: Conversation) -> str:
        preview = getattr(obj, "last_message", None)
        if preview is None:
            last = obj.messages.order_by("-created_at").values_list("content", flat=True).first()
            preview = last or ""
        return str(preview)[:160]

    def get_messageCount(self, obj: Conversation) -> int:
        count = getattr(obj, "message_count", None)
        return int(count if count is not None else obj.messages.count())

    def get_hasAttachments(self, obj: Conversation) -> bool:
        flag = getattr(obj, "has_attachments", None)
        return bool(flag if flag is not None else obj.attachments.exists())

    def create(self, validated_data: dict) -> Conversation:
        from django.utils import timezone

        archived = validated_data.pop("archived", False)
        conversation = super().create(validated_data)
        if archived:
            conversation.archived_at = timezone.now()
            conversation.save(update_fields=["archived_at"])
        return conversation

    def update(self, instance: Conversation, validated_data: dict) -> Conversation:
        from django.utils import timezone

        if "archived" in validated_data:
            archived = validated_data.pop("archived")
            if archived and instance.archived_at is None:
                instance.archived_at = timezone.now()
            elif not archived:
                instance.archived_at = None
        return super().update(instance, validated_data)


class MessageSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    conversationId = serializers.UUIDField(source="conversation_id", read_only=True)
    status = serializers.SerializerMethodField()
    model = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ("id", "conversationId", "role", "content", "status", "model", "metadata", "createdAt")
        read_only_fields = fields

    def get_status(self, obj: Message) -> str:
        return str((obj.metadata or {}).get("status") or "complete")

    def get_model(self, obj: Message) -> str | None:
        metadata = obj.metadata or {}
        return metadata.get("modelAlias") or metadata.get("model")


class ChatRequestCreateSerializer(serializers.Serializer):
    conversationId = serializers.UUIDField()
    chatInput = serializers.CharField(max_length=50_000, trim_whitespace=False)
    timezone = serializers.CharField(max_length=100, required=False, allow_blank=True)
    locale = serializers.CharField(max_length=32, required=False, allow_blank=True)


class ConversationMessageCreateSerializer(serializers.Serializer):
    content = serializers.CharField(max_length=50_000, trim_whitespace=False)
    timezone = serializers.CharField(max_length=100, required=False, allow_blank=True)
    locale = serializers.CharField(max_length=32, required=False, allow_blank=True)


class ChatRequestListQuerySerializer(serializers.Serializer):
    conversationId = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(choices=ChatRequest.Status.choices, required=False)


class ChatRequestSerializer(serializers.ModelSerializer):
    conversationId = serializers.UUIDField(source="conversation_id", read_only=True)
    taskType = serializers.CharField(source="task_type", read_only=True)
    inputText = serializers.CharField(source="input_text", read_only=True)
    outputText = serializers.CharField(source="output_text", read_only=True)
    errorCode = serializers.CharField(source="error_code", read_only=True)
    errorMessage = serializers.CharField(source="error_message", read_only=True)
    traceId = serializers.CharField(source="trace_id", read_only=True)
    retryCount = serializers.IntegerField(source="retry_count", read_only=True)
    maxRetries = serializers.IntegerField(source="max_retries", read_only=True)
    cancelRequestedAt = serializers.DateTimeField(source="cancel_requested_at", read_only=True)
    startedAt = serializers.DateTimeField(source="started_at", read_only=True)
    completedAt = serializers.DateTimeField(source="completed_at", read_only=True)
    providerName = serializers.CharField(source="provider_name", read_only=True)
    modelName = serializers.CharField(source="model_name", read_only=True)
    modelRunId = serializers.UUIDField(source="model_run_id", read_only=True, allow_null=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = ChatRequest
        fields = (
            "id",
            "conversationId",
            "status",
            "taskType",
            "inputText",
            "outputText",
            "errorCode",
            "errorMessage",
            "traceId",
            "retryCount",
            "maxRetries",
            "cancelRequestedAt",
            "startedAt",
            "completedAt",
            "providerName",
            "modelName",
            "modelRunId",
            "createdAt",
            "updatedAt",
        )
        read_only_fields = fields


class ConversationFeedbackCreateSerializer(serializers.Serializer):
    chatRequestId = serializers.UUIDField(required=False)
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(max_length=10_000, required=False, allow_blank=True)
    metadata = serializers.JSONField(required=False)


class ConversationFeedbackSerializer(serializers.ModelSerializer):
    chatRequestId = serializers.UUIDField(source="chat_request_id", read_only=True, allow_null=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = ConversationFeedback
        fields = ("id", "chatRequestId", "rating", "comment", "metadata", "createdAt", "updatedAt")
        read_only_fields = fields
