from rest_framework import serializers
from apps.governance.models import AuditEvent, ConsentRecord, RetentionRule, SafetyEvent, SupportCase


class AuditEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source='actor.email', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    severity_display = serializers.CharField(source='get_severity_display', read_only=True)

    class Meta:
        model = AuditEvent
        fields = [
            'id', 'organization', 'organization_name', 'actor', 'actor_email',
            'category', 'category_display', 'action', 'resource_type', 'resource_id',
            'severity', 'severity_display', 'description', 'metadata',
            'ip_address', 'user_agent', 'trace_id', 'request_id', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class ConsentRecordSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    consent_type_display = serializers.CharField(source='get_consent_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = ConsentRecord
        fields = [
            'id', 'organization', 'organization_name', 'user', 'user_email',
            'consent_type', 'consent_type_display', 'status', 'status_display',
            'version', 'ip_address', 'user_agent', 'metadata',
            'granted_at', 'withdrawn_at', 'expires_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ConsentUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ConsentRecord.Status.choices)
    version = serializers.CharField(max_length=50, required=False)


class RetentionRuleSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    data_category_display = serializers.CharField(source='get_data_category_display', read_only=True)
    action_display = serializers.CharField(source='get_action_display', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = RetentionRule
        fields = [
            'id', 'organization', 'organization_name', 'data_category',
            'data_category_display', 'action', 'action_display',
            'retention_days', 'grace_period_days', 'is_active', 'legal_hold',
            'description', 'metadata', 'created_by', 'created_by_email',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class RetentionRuleUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = RetentionRule
        fields = ['action', 'retention_days', 'grace_period_days', 'is_active', 'legal_hold', 'description', 'metadata']


class SafetyEventSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    severity_display = serializers.CharField(source='get_severity_display', read_only=True)
    action_taken_display = serializers.CharField(source='get_action_taken_display', read_only=True)
    reviewed_by_email = serializers.EmailField(source='reviewed_by.email', read_only=True)

    class Meta:
        model = SafetyEvent
        fields = [
            'id', 'organization', 'organization_name', 'user', 'user_email',
            'job', 'category', 'category_display', 'severity', 'severity_display',
            'action_taken', 'action_taken_display', 'description', 'evidence',
            'model_provider', 'model_name', 'prompt_hash', 'response_hash',
            'trace_id', 'request_id', 'reviewed_by', 'reviewed_by_email',
            'reviewed_at', 'review_notes', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'prompt_hash', 'response_hash'
        ]


class SafetyEventReviewSerializer(serializers.Serializer):
    action_taken = serializers.ChoiceField(choices=SafetyEvent.Action.choices)
    review_notes = serializers.CharField(required=False, allow_blank=True)


class SupportCaseSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    assigned_to_email = serializers.EmailField(source='assigned_to.email', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    priority_display = serializers.CharField(source='get_priority_display', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)

    class Meta:
        model = SupportCase
        fields = [
            'id', 'organization', 'organization_name', 'user', 'user_email',
            'status', 'status_display', 'priority', 'priority_display',
            'category', 'category_display', 'subject', 'description',
            'assigned_to', 'assigned_to_email', 'tags', 'metadata',
            'resolved_at', 'closed_at', 'first_response_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'resolved_at', 'closed_at', 'first_response_at',
            'created_at', 'updated_at'
        ]


class SupportCaseCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportCase
        fields = ['subject', 'description', 'priority', 'category', 'tags']


class SupportCaseUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportCase
        fields = ['status', 'priority', 'category', 'assigned_to', 'tags', 'metadata']