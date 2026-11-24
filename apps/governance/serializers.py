from rest_framework import serializers

from apps.governance.models import (
    AuditEvent,
    ConsentRecord,
    RetentionRule,
    SafetyEvent,
    SupportCase,
    SupportCaseMessage,
)


class AuditEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    severity_display = serializers.CharField(source="get_severity_display", read_only=True)

    class Meta:
        model = AuditEvent
        fields = [
            "id",
            "organization",
            "organization_name",
            "actor",
            "actor_email",
            "category",
            "category_display",
            "action",
            "resource_type",
            "resource_id",
            "severity",
            "severity_display",
            "outcome",
            "description",
            "metadata",
            "ip_address",
            "user_agent",
            "trace_id",
            "request_id",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class ConsentRecordSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    consent_type_display = serializers.CharField(source="get_consent_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = ConsentRecord
        fields = [
            "id",
            "organization",
            "organization_name",
            "user",
            "user_email",
            "consent_type",
            "consent_type_display",
            "status",
            "status_display",
            "version",
            "ip_address",
            "user_agent",
            "metadata",
            "granted_at",
            "withdrawn_at",
            "expires_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ConsentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConsentRecord
        fields = ["consent_type", "status", "version", "metadata", "expires_at"]


class ConsentUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ConsentRecord.Status.choices)
    version = serializers.CharField(max_length=50, required=False)


def _retention_floor_days(data_category: str) -> int:
    """Legal minimum retention: audit and safety records may not be purged early."""
    from django.conf import settings

    if data_category == RetentionRule.DataCategory.AUDIT_EVENTS:
        return int(settings.AUDIT_EVENT_RETENTION_DAYS)
    return 1


class _RetentionFloorMixin:
    def validate(self, attrs):
        attrs = super().validate(attrs)  # type: ignore[misc]
        instance = getattr(self, "instance", None)
        category = attrs.get("data_category") or getattr(instance, "data_category", "")
        retention = attrs.get("retention_days", getattr(instance, "retention_days", None))
        floor = _retention_floor_days(category)
        if retention is not None and retention < floor:
            raise serializers.ValidationError(
                {"retention_days": f"Records in this category must be kept for at least {floor} days."}
            )
        return attrs


class RetentionRuleSerializer(_RetentionFloorMixin, serializers.ModelSerializer):
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    data_category_display = serializers.CharField(source="get_data_category_display", read_only=True)
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)

    class Meta:
        model = RetentionRule
        fields = [
            "id",
            "organization",
            "organization_name",
            "data_category",
            "data_category_display",
            "action",
            "action_display",
            "retention_days",
            "grace_period_days",
            "is_active",
            "legal_hold",
            "description",
            "metadata",
            "created_by",
            "created_by_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "organization", "created_by", "created_at", "updated_at"]


class RetentionRuleUpdateSerializer(_RetentionFloorMixin, serializers.ModelSerializer):
    class Meta:
        model = RetentionRule
        fields = [
            "action",
            "retention_days",
            "grace_period_days",
            "is_active",
            "legal_hold",
            "description",
            "metadata",
        ]


class SafetyEventSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    severity_display = serializers.CharField(source="get_severity_display", read_only=True)
    action_taken_display = serializers.CharField(source="get_action_taken_display", read_only=True)
    reviewed_by_email = serializers.EmailField(source="reviewed_by.email", read_only=True)

    class Meta:
        model = SafetyEvent
        fields = [
            "id",
            "organization",
            "organization_name",
            "user",
            "user_email",
            "job",
            "category",
            "category_display",
            "severity",
            "severity_display",
            "action_taken",
            "action_taken_display",
            "description",
            "evidence",
            "model_provider",
            "model_name",
            "prompt_hash",
            "response_hash",
            "trace_id",
            "request_id",
            "reviewed_by",
            "reviewed_by_email",
            "reviewed_at",
            "review_notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "organization",
            "user",
            "reviewed_by",
            "reviewed_at",
            "id",
            "created_at",
            "updated_at",
            "prompt_hash",
            "response_hash",
        ]


class SafetyEventReviewSerializer(serializers.Serializer):
    action_taken = serializers.ChoiceField(choices=SafetyEvent.Action.choices)
    review_notes = serializers.CharField(required=False, allow_blank=True)


class SupportCaseMessageSerializer(serializers.ModelSerializer):
    """A reply as the customer sees it: support staff appear as "JT-Code support", not by name."""

    author_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportCaseMessage
        fields = ["id", "author_role", "author_name", "body", "created_at"]
        read_only_fields = fields

    def get_author_name(self, obj) -> str:
        if obj.author_role == SupportCaseMessage.Author.SUPPORT:
            return "JT-Code support"
        author = obj.author
        return (author.full_name or author.email) if author else ""


class SupportCaseSerializer(serializers.ModelSerializer):
    """A case as members see it. Everything is read-only: customers act through ``messages`` and ``close``."""

    user_email = serializers.EmailField(source="user.email", read_only=True)
    message_count = serializers.IntegerField(source="messages.count", read_only=True)

    class Meta:
        model = SupportCase
        fields = [
            "id",
            "user_email",
            "status",
            "priority",
            "category",
            "subject",
            "description",
            "message_count",
            "resolved_at",
            "closed_at",
            "first_response_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class SupportCaseCreateSerializer(serializers.ModelSerializer):
    subject = serializers.CharField(max_length=200)
    description = serializers.CharField(max_length=10000)
    priority = serializers.ChoiceField(
        choices=SupportCase.Priority.choices, default=SupportCase.Priority.MEDIUM
    )
    category = serializers.ChoiceField(choices=SupportCase.Category.choices)

    class Meta:
        model = SupportCase
        fields = ["subject", "description", "priority", "category"]


class SupportCaseReplySerializer(serializers.Serializer):
    body = serializers.CharField(max_length=10000)
