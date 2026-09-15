from rest_framework import serializers
from apps.conversations.models import ChatRequest, Conversation

class ConversationSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)
    class Meta:
        model = Conversation
        fields = ('id', 'title', 'createdAt', 'updatedAt')

class ChatRequestCreateSerializer(serializers.Serializer):
    conversationId = serializers.UUIDField()
    chatInput = serializers.CharField(max_length=50_000, trim_whitespace=False)
    timezone = serializers.CharField(max_length=100, required=False, allow_blank=True)
    locale = serializers.CharField(max_length=32, required=False, allow_blank=True)

class ChatRequestSerializer(serializers.ModelSerializer):
    conversationId = serializers.UUIDField(source='conversation_id', read_only=True)
    taskType = serializers.CharField(source='task_type', read_only=True)
    inputText = serializers.CharField(source='input_text', read_only=True)
    outputText = serializers.CharField(source='output_text', read_only=True)
    errorCode = serializers.CharField(source='error_code', read_only=True)
    traceId = serializers.CharField(source='trace_id', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)
    class Meta:
        model = ChatRequest
        fields = ('id', 'conversationId', 'status', 'taskType', 'inputText', 'outputText', 'errorCode', 'traceId', 'createdAt', 'updatedAt')
