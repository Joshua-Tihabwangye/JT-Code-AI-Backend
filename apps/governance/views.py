from __future__ import annotations

from datetime import UTC, datetime, time

from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.views import APIView
from apps.governance.models import AuditEvent, ConsentRecord, RetentionRule, SafetyEvent, SupportCase
from apps.governance.serializers import (
    AuditEventSerializer,
    ConsentCreateSerializer,
    ConsentRecordSerializer,
    ConsentUpdateSerializer,
    RetentionRuleSerializer,
    RetentionRuleUpdateSerializer,
    SafetyEventReviewSerializer,
    SafetyEventSerializer,
    SupportCaseCreateSerializer,
    SupportCaseSerializer,
    SupportCaseUpdateSerializer,
)
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    IsOrganizationAdmin,
    organization_for_request,
)


def _tenant_queryset(queryset, request: Request, *, organization_field: str = "organization"):
    organization = organization_for_request(request)
    if organization is None:
        return queryset.none()
    return queryset.filter(**{f"{organization_field}_id": organization.id})


class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    """The tenant audit trail: organization admins only (it contains IPs and actors)."""

    permission_classes = [IsAuthenticated, IsOrganizationAdmin]
    serializer_class = AuditEventSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = _tenant_queryset(AuditEvent.objects.all(), self.request).select_related(
            "organization", "actor"
        )

        # Filter by category
        category = self.request.query_params.get("category")
        if category:
            queryset = queryset.filter(category=category)

        # Filter by severity
        severity = self.request.query_params.get("severity")
        if severity:
            queryset = queryset.filter(severity=severity)

        # Filter by actor
        actor_id = self.request.query_params.get("actor")
        if actor_id:
            queryset = queryset.filter(actor_id=actor_id)

        for name, lookup in (("start_date", "created_at__gte"), ("end_date", "created_at__lte")):
            if value := self.request.query_params.get(name):
                moment = parse_datetime(value) or (
                    datetime.combine(day, time.min, tzinfo=UTC) if (day := parse_date(value)) else None
                )
                if moment is None:
                    raise ValidationError({name: "Use an ISO-8601 date or datetime."})
                queryset = queryset.filter(**{lookup: moment})
        if outcome := self.request.query_params.get("outcome"):
            queryset = queryset.filter(outcome=outcome)
        return queryset


class ConsentRecordViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = ConsentRecordSerializer
    lookup_field = "id"

    def get_permissions(self):
        # Consent is a member's own record; management of existing tenant
        # records still requires an editor or admin role.
        if self.action == "create":
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        return _tenant_queryset(ConsentRecord.objects.all(), self.request).select_related(
            "organization", "user"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return ConsentCreateSerializer
        if self.action in ["update", "partial_update"]:
            return ConsentUpdateSerializer
        return ConsentRecordSerializer

    def perform_create(self, serializer):
        serializer.save(
            organization=organization_for_request(self.request, required=True),
            user=self.request.user,
        )


class RetentionRuleViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = RetentionRuleSerializer
    lookup_field = "id"

    def get_queryset(self):
        return _tenant_queryset(RetentionRule.objects.all(), self.request).select_related(
            "organization", "created_by"
        )

    def get_serializer_class(self):
        if self.action in ["update", "partial_update"]:
            return RetentionRuleUpdateSerializer
        return RetentionRuleSerializer

    def perform_create(self, serializer):
        serializer.save(
            organization=organization_for_request(self.request, required=True),
            created_by=self.request.user,
        )


class SafetyEventViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = SafetyEventSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = _tenant_queryset(SafetyEvent.objects.all(), self.request).select_related(
            "organization", "user", "job", "reviewed_by"
        )

        # Filter by category
        category = self.request.query_params.get("category")
        if category:
            queryset = queryset.filter(category=category)

        # Filter by severity
        severity = self.request.query_params.get("severity")
        if severity:
            queryset = queryset.filter(severity=severity)

        # Filter by action taken
        action = self.request.query_params.get("action_taken")
        if action:
            queryset = queryset.filter(action_taken=action)

        return queryset

    @action(detail=True, methods=["post"])
    def review(self, request: Request, id=None):
        event = self.get_object()
        serializer = SafetyEventReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        event.action_taken = serializer.validated_data["action_taken"]
        event.reviewed_by = request.user
        event.reviewed_at = timezone.now()
        event.review_notes = serializer.validated_data.get("review_notes", "")
        event.save(update_fields=["action_taken", "reviewed_by", "reviewed_at", "review_notes"])

        return Response(SafetyEventSerializer(event).data)


class SupportCaseViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = SupportCaseSerializer
    lookup_field = "id"

    def get_permissions(self):
        # Any member can open a case for their selected tenant. Assignment,
        # resolution, and other management actions remain role-protected.
        if self.action == "create":
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = _tenant_queryset(SupportCase.objects.all(), self.request).select_related(
            "organization", "user", "assigned_to"
        )

        # Filter by status
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        # Filter by priority
        priority = self.request.query_params.get("priority")
        if priority:
            queryset = queryset.filter(priority=priority)

        return queryset

    def get_serializer_class(self):
        if self.action == "create":
            return SupportCaseCreateSerializer
        if self.action in ["update", "partial_update"]:
            return SupportCaseUpdateSerializer
        return SupportCaseSerializer

    def perform_create(self, serializer):
        serializer.save(
            organization=organization_for_request(self.request, required=True), user=self.request.user
        )

    @action(detail=True, methods=["post"])
    def assign(self, request: Request, id=None):
        case = self.get_object()
        assigned_to_id = request.data.get("assigned_to_id")
        if not assigned_to_id:
            return Response({"detail": "assigned_to_id required"}, status=status.HTTP_400_BAD_REQUEST)

        from django.contrib.auth import get_user_model

        User = get_user_model()
        try:
            user = User.objects.get(id=assigned_to_id)
        except User.DoesNotExist:
            return Response({"detail": "User not found"}, status=status.HTTP_404_NOT_FOUND)

        case.assigned_to = user
        case.status = SupportCase.Status.IN_PROGRESS
        if not case.first_response_at:
            case.first_response_at = timezone.now()
        case.save(update_fields=["assigned_to", "status", "first_response_at"])

        return Response(SupportCaseSerializer(case).data)

    @action(detail=True, methods=["post"])
    def resolve(self, request: Request, id=None):
        case = self.get_object()
        case.status = SupportCase.Status.RESOLVED
        case.resolved_at = timezone.now()
        case.save(update_fields=["status", "resolved_at"])
        return Response(SupportCaseSerializer(case).data)

    @action(detail=True, methods=["post"])
    def close(self, request: Request, id=None):
        case = self.get_object()
        case.status = SupportCase.Status.CLOSED
        case.closed_at = timezone.now()
        case.save(update_fields=["status", "closed_at"])
        return Response(SupportCaseSerializer(case).data)


class GovernanceDashboardView(APIView):
    """Dashboard statistics for governance"""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        organization = organization_for_request(request, required=True)

        # Safety events stats
        safety_stats = (
            SafetyEvent.objects.filter(organization=organization)
            .values("category", "severity", "action_taken")
            .annotate(count=Count("id"))
        )

        # Support cases stats
        case_stats = (
            SupportCase.objects.filter(organization=organization)
            .values("status", "priority", "category")
            .annotate(count=Count("id"))
        )

        # Audit events stats (last 30 days)
        from django.utils import timezone

        thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
        audit_stats = (
            AuditEvent.objects.filter(organization=organization, created_at__gte=thirty_days_ago)
            .values("category", "severity")
            .annotate(count=Count("id"))
        )

        # Consent stats
        consent_stats = (
            ConsentRecord.objects.filter(organization=organization)
            .values("consent_type", "status")
            .annotate(count=Count("id"))
        )

        return Response(
            {
                "safety_events": list(safety_stats),
                "support_cases": list(case_stats),
                "audit_events_30d": list(audit_stats),
                "consents": list(consent_stats),
            }
        )
