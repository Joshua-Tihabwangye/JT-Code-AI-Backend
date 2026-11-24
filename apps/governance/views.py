from __future__ import annotations

import csv
import uuid
from datetime import UTC, datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import Throttled, ValidationError
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
    SupportCaseMessageSerializer,
    SupportCaseReplySerializer,
    SupportCaseSerializer,
)
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    IsOrganizationAdmin,
    organization_for_request,
    user_has_role,
)
from apps.identity.models import Role


def _tenant_queryset(queryset, request: Request, *, organization_field: str = "organization"):
    organization = organization_for_request(request)
    if organization is None:
        return queryset.none()
    return queryset.filter(**{f"{organization_field}_id": organization.id})


AUDIT_EXPORT_MAX_ROWS = 50_000
AUDIT_EXPORT_FIELDS = (
    "created_at",
    "actor_email",
    "category",
    "action",
    "severity",
    "outcome",
    "resource_type",
    "resource_id",
    "description",
    "ip_address",
    "user_agent",
    "request_id",
)
# A cell starting with one of these is a formula in Excel, LibreOffice and Google Sheets.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_START) else text


class _Echo:
    def write(self, value: str) -> str:
        return value


class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    """The tenant audit trail: organization admins only (it contains IPs and actors)."""

    permission_classes = [IsAuthenticated, IsOrganizationAdmin]
    serializer_class = AuditEventSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = _tenant_queryset(AuditEvent.objects.all(), self.request).select_related(
            "organization", "actor"
        )
        params = self.request.query_params
        for name in ("category", "severity", "outcome"):
            if value := params.get(name):
                queryset = queryset.filter(**{name: value})
        if actor_id := params.get("actor"):
            try:
                queryset = queryset.filter(actor_id=uuid.UUID(actor_id))
            except ValueError as exc:
                raise ValidationError({"actor": "Use a user id."}) from exc
        if value := params.get("start_date"):
            queryset = queryset.filter(created_at__gte=self._moment("start_date", value))
        if value := params.get("end_date"):
            # A date-only end includes that whole day; a datetime is an exact upper bound.
            # (parse_datetime also accepts a bare date, as midnight, which dropped the day's own events.)
            try:
                day = parse_date(value) if len(value) == 10 else None
            except ValueError as exc:
                raise ValidationError({"end_date": "Use an ISO-8601 date or datetime."}) from exc
            if day is not None:
                queryset = queryset.filter(
                    created_at__lt=datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)
                )
            else:
                queryset = queryset.filter(created_at__lte=self._moment("end_date", value))
        return queryset.order_by("-created_at")

    @staticmethod
    def _moment(name: str, value: str) -> datetime:
        try:
            moment = parse_datetime(value) or (
                datetime.combine(day, time.min, tzinfo=UTC) if (day := parse_date(value)) else None
            )
        except ValueError:
            moment = None
        if moment is None:
            raise ValidationError({name: "Use an ISO-8601 date or datetime."})
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)

    @action(detail=False, methods=["get"])
    def export(self, request: Request):
        """CSV of the filtered trail (newest first, at most AUDIT_EXPORT_MAX_ROWS rows), formula-safe."""
        writer = csv.writer(_Echo())
        rows = self.get_queryset()[:AUDIT_EXPORT_MAX_ROWS].iterator(chunk_size=2000)

        def lines():
            yield writer.writerow(AUDIT_EXPORT_FIELDS)
            for event in rows:
                values = {
                    "created_at": event.created_at.isoformat(),
                    "actor_email": event.actor.email if event.actor_id else "",
                }
                yield writer.writerow(
                    [_csv_cell(values.get(field, getattr(event, field, ""))) for field in AUDIT_EXPORT_FIELDS]
                )

        response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        response["Content-Disposition"] = f'attachment; filename="audit-log-{stamp}.csv"'
        return response


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
    # Retention rules delete data (including the audit trail), so only admins manage them.
    permission_classes = [IsAuthenticated, IsOrganizationAdmin]
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

    def perform_create(self, serializer):
        # Tenant and reporter come from the request, never the payload.
        organization = organization_for_request(self.request, required=True)
        job = serializer.validated_data.get("job")
        if job is not None and job.organization_id != organization.id:
            raise ValidationError({"job": "The job does not belong to this organization."})
        serializer.save(organization=organization, user=self.request.user)

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
    """Support cases from the customer's side. JT-Code support answers in the Django admin.

    Any member opens cases and sees their own; organization admins see every case of the organization.
    Customers reply and close; status, priority, assignment and resolution belong to support staff.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = SupportCaseSerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        organization = organization_for_request(self.request, required=True)
        queryset = SupportCase.objects.filter(organization=organization).select_related("user")
        if not user_has_role(self.request.user, Role.RoleType.ADMIN, organization.id):
            queryset = queryset.filter(user=self.request.user)
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset.order_by("-updated_at")

    def get_serializer_class(self):
        if self.action == "create":
            return SupportCaseCreateSerializer
        return SupportCaseSerializer

    def create(self, request, *args, **kwargs):
        from apps.core.ratelimit import hit
        from apps.governance.support import after_commit, notify_support

        serializer = SupportCaseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not hit(
            f"support-case:{request.user.pk}", limit=settings.SUPPORT_CASES_PER_HOUR, window=3600
        ).allowed:
            raise Throttled(detail="Too many support cases this hour. Reply to an open case instead.")
        with transaction.atomic():
            case = serializer.save(
                organization=organization_for_request(request, required=True), user=request.user
            )
            after_commit(notify_support, str(case.id))
        return Response(SupportCaseSerializer(case).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"])
    def messages(self, request: Request, id=None):
        case = self.get_object()
        if request.method == "GET":
            return Response(
                SupportCaseMessageSerializer(case.messages.select_related("author"), many=True).data
            )
        if case.status == SupportCase.Status.CLOSED:
            return Response(
                {"detail": "This case is closed. Open a new one."}, status=status.HTTP_409_CONFLICT
            )
        reply = SupportCaseReplySerializer(data=request.data)
        reply.is_valid(raise_exception=True)
        from apps.governance.models import SupportCaseMessage
        from apps.governance.support import add_message

        with transaction.atomic():
            message = add_message(
                case, request.user, SupportCaseMessage.Author.CUSTOMER, reply.validated_data["body"]
            )
        return Response(SupportCaseMessageSerializer(message).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def close(self, request: Request, id=None):
        case = self.get_object()
        if case.status != SupportCase.Status.CLOSED:
            case.status = SupportCase.Status.CLOSED
            case.closed_at = timezone.now()
            case.save(update_fields=["status", "closed_at", "updated_at"])
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
