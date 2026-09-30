from rest_framework import serializers

from apps.conversations.models import ChatRequest, Conversation, ConversationFeedback, Message


class ConversationSerializer(serializers.ModelSerializer):
    archivedAt = serializers.DateTimeField(source="archived_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Conversation
        fields = ("id", "title", "archivedAt", "createdAt", "updatedAt")
        read_only_fields = ("id", "archivedAt", "createdAt", "updatedAt")


class MessageSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Message
        fields = ("id", "role", "content", "metadata", "createdAt")
        read_only_fields = fields


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
