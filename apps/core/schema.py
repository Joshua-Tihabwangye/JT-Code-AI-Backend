"""OpenAPI extensions and explicit contracts for APIViews that do not use DRF generics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.types import OpenApiTypes
from rest_framework import serializers

from apps.assets.serializers import AssetSerializer, CompleteUploadSerializer, SignatureRequestSerializer
from apps.identity.serializers import OrganizationSerializer

if TYPE_CHECKING:
    # DRF serializers are generic only for type checkers (djangorestframework-stubs).
    _Serializer = serializers.Serializer[Any]
else:
    _Serializer = serializers.Serializer


class SupabaseJWTAuthenticationScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    """Describe the project authentication class once for every v1 operation."""

    target_class = "apps.identity.authentication.SupabaseJWTAuthentication"
    name = "SupabaseBearer"
    priority = 1

    def get_security_definition(self, auto_schema: AutoSchema) -> dict[str, str]:
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Supabase access token.",
        }


class DetailResponseSerializer(_Serializer):
    detail = serializers.CharField()


class EmptyRequestSerializer(_Serializer):
    pass


class HealthResponseSerializer(_Serializer):
    status = serializers.ChoiceField(choices=("ok", "degraded"))
    service = serializers.CharField(required=False)
    checks = serializers.DictField(child=serializers.CharField(), required=False)


class RelayRequestSerializer(_Serializer):
    message = serializers.CharField(required=False)
    workflowId = serializers.CharField(required=False)
    executionId = serializers.CharField(required=False)
    step = serializers.CharField(required=False)
    errorCode = serializers.CharField(required=False)


class ImageKitSignatureResponseSerializer(_Serializer):
    publicKey = serializers.CharField()
    endpointUrl = serializers.URLField()
    uploadUrl = serializers.URLField()
    folder = serializers.CharField()
    fileName = serializers.CharField()
    token = serializers.CharField()
    expire = serializers.IntegerField()
    signature = serializers.CharField()


class CompletionRequestSerializer(_Serializer):
    task_type = serializers.CharField(required=False)
    messages = serializers.ListField(child=serializers.DictField(), min_length=1)
    model_id = serializers.UUIDField(required=False)
    policy_slug = serializers.CharField(required=False)
    stream = serializers.BooleanField(required=False)
    temperature = serializers.FloatField(required=False)
    max_tokens = serializers.IntegerField(min_value=1, required=False)
    tools = serializers.ListField(child=serializers.DictField(), required=False)


class QueuedJobResponseSerializer(_Serializer):
    job_id = serializers.UUIDField()
    request_id = serializers.UUIDField()
    status = serializers.CharField()


class CompletionResponseSerializer(QueuedJobResponseSerializer):
    model = serializers.CharField()


class EmbeddingRequestSerializer(_Serializer):
    texts = serializers.ListField(child=serializers.CharField(), min_length=1)
    model_id = serializers.UUIDField(required=False)


class AvailableModelsResponseSerializer(_Serializer):
    models = serializers.ListField(child=serializers.DictField())
    plan = serializers.CharField()


class ImageGenerationRequestSerializer(_Serializer):
    prompt = serializers.CharField(min_length=1)
    n = serializers.IntegerField(min_value=1, max_value=4, required=False)
    size = serializers.ChoiceField(choices=("256x256", "512x512", "1024x1024"), required=False)
    model_id = serializers.UUIDField(required=False)


class ImageEditRequestSerializer(_Serializer):
    prompt = serializers.CharField(min_length=1)
    file = serializers.FileField()
    size = serializers.ChoiceField(choices=("256x256", "512x512", "1024x1024"), required=False)
    model_id = serializers.UUIDField(required=False)


class ImageUnderstandingRequestSerializer(_Serializer):
    file = serializers.FileField()
    prompt = serializers.CharField(required=False)
    model_id = serializers.UUIDField(required=False)


class GeneratedImageSerializer(_Serializer):
    id = serializers.UUIDField()
    url = serializers.CharField()


class ImageGenerationResponseSerializer(_Serializer):
    # A declared field named ``data`` is valid DRF; it only shadows ``Serializer.data`` for type checkers.
    data = GeneratedImageSerializer(many=True)  # type: ignore[assignment]
    request_id = serializers.UUIDField()


class ImageUnderstandingResponseSerializer(_Serializer):
    description = serializers.CharField()
    request_id = serializers.UUIDField()


class JobStatusCallbackRequestSerializer(_Serializer):
    status = serializers.CharField()
    result = serializers.JSONField(required=False)
    error_code = serializers.CharField(required=False)
    error_message = serializers.CharField(required=False)
    progress_percent = serializers.IntegerField(min_value=0, max_value=100, required=False)


class JobStatusCallbackResponseSerializer(_Serializer):
    id = serializers.UUIDField()
    status = serializers.CharField()


class ResearchJobRequestSerializer(_Serializer):
    query = serializers.CharField(min_length=1)
    sources = serializers.ListField(child=serializers.URLField(), required=False)
    callback_url = serializers.URLField(required=False)


class SearchRequestSerializer(_Serializer):
    query = serializers.CharField(min_length=1)
    collection_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    top_k = serializers.IntegerField(min_value=1, required=False)
    min_similarity = serializers.FloatField(min_value=0, max_value=1, required=False)


class SearchResponseSerializer(_Serializer):
    query = serializers.CharField(required=False)
    collections = serializers.ListField(child=serializers.UUIDField(), required=False)
    results = serializers.ListField(child=serializers.DictField())
    result_count = serializers.IntegerField(required=False)
    message = serializers.CharField(required=False)


class RAGQueryRequestSerializer(_Serializer):
    query = serializers.CharField(min_length=1)
    collection_ids = serializers.ListField(child=serializers.UUIDField(), min_length=1)
    conversation_id = serializers.UUIDField(required=False)
    include_citations = serializers.BooleanField(required=False)


class AuthPingResponseSerializer(_Serializer):
    authenticated = serializers.BooleanField()
    user_id = serializers.UUIDField()
    email = serializers.EmailField()


class OrganizationMemberSerializer(_Serializer):
    user_id = serializers.UUIDField()
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=("admin", "editor", "viewer"))


class OrganizationMembersResponseSerializer(_Serializer):
    results = OrganizationMemberSerializer(many=True)


class OrganizationInviteRequestSerializer(_Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=("admin", "editor", "viewer"), required=False)


class OrganizationInviteResponseSerializer(_Serializer):
    id = serializers.UUIDField()
    email = serializers.EmailField()
    role = serializers.CharField()
    token = serializers.CharField()
    expires_at = serializers.DateTimeField()


class OrganizationRoleRequestSerializer(_Serializer):
    role = serializers.ChoiceField(choices=("admin", "editor", "viewer"))


class ConsentRequestSerializer(_Serializer):
    consent_type = serializers.CharField()
    status = serializers.ChoiceField(choices=("granted", "denied"))


class ConsentListResponseSerializer(_Serializer):
    results = serializers.ListField(child=serializers.DictField())


class UsageResponseSerializer(_Serializer):
    wallet = serializers.DictField(allow_null=True)
    subscription = serializers.DictField(allow_null=True)
    recent_usage = serializers.ListField(child=serializers.DictField())
    usage_by_type_30d = serializers.ListField(child=serializers.DictField())


class StripeWebhookResponseSerializer(_Serializer):
    received = serializers.BooleanField()


class IncomingWebhookResponseSerializer(_Serializer):
    received = serializers.BooleanField()
    delivery_id = serializers.UUIDField()


class DashboardResponseSerializer(_Serializer):
    metrics = serializers.DictField()
    recent_events = serializers.ListField(child=serializers.DictField())


@dataclass(frozen=True)
class Contract:
    request: dict[str, type[_Serializer] | OpenApiTypes | None]
    responses: dict[str, dict[int, type[_Serializer] | OpenApiTypes | None]]


CONTRACTS: dict[str, Contract] = {
    "apps.core.views.LiveView": Contract({}, {"get": {200: HealthResponseSerializer}}),
    "apps.core.views.StartupView": Contract({}, {"get": {200: HealthResponseSerializer}}),
    "apps.core.views.ReadyView": Contract(
        {}, {"get": {200: HealthResponseSerializer, 503: HealthResponseSerializer}}
    ),
    "apps.core.views.N8nSentryRelayView": Contract(
        {"post": RelayRequestSerializer}, {"post": {202: None, 401: DetailResponseSerializer}}
    ),
    "apps.assets.views.ImageKitSignatureView": Contract(
        {"post": SignatureRequestSerializer},
        {
            "post": {
                200: ImageKitSignatureResponseSerializer,
                413: DetailResponseSerializer,
                503: DetailResponseSerializer,
            }
        },
    ),
    "apps.assets.views.CompleteUploadView": Contract(
        {"post": CompleteUploadSerializer},
        {"post": {201: AssetSerializer}},
    ),
    "apps.ai_gateway.views.CompletionView": Contract(
        {"post": CompletionRequestSerializer}, {"post": {202: CompletionResponseSerializer}}
    ),
    "apps.ai_gateway.views.EmbeddingView": Contract(
        {"post": EmbeddingRequestSerializer}, {"post": {202: QueuedJobResponseSerializer}}
    ),
    "apps.ai_gateway.views.AIModelsView": Contract({}, {"get": {200: AvailableModelsResponseSerializer}}),
    "apps.ai_gateway.image_views.ImageGenerationView": Contract(
        {"post": ImageGenerationRequestSerializer}, {"post": {200: ImageGenerationResponseSerializer}}
    ),
    "apps.ai_gateway.image_views.ImageEditView": Contract(
        {"post": ImageEditRequestSerializer}, {"post": {200: ImageGenerationResponseSerializer}}
    ),
    "apps.ai_gateway.image_views.ImageUnderstandingView": Contract(
        {"post": ImageUnderstandingRequestSerializer}, {"post": {200: ImageUnderstandingResponseSerializer}}
    ),
    "apps.jobs.views.JobStatusCallbackView": Contract(
        {"post": JobStatusCallbackRequestSerializer}, {"post": {200: JobStatusCallbackResponseSerializer}}
    ),
    "apps.jobs.views.ResearchJobsView": Contract(
        {"post": ResearchJobRequestSerializer}, {"post": {202: QueuedJobResponseSerializer}}
    ),
    "apps.knowledge.views.SearchView": Contract(
        {"post": SearchRequestSerializer}, {"post": {200: SearchResponseSerializer}}
    ),
    "apps.knowledge.views.RAGQueryView": Contract(
        {"post": RAGQueryRequestSerializer}, {"post": {202: QueuedJobResponseSerializer}}
    ),
    "apps.identity.views.AuthPingView": Contract({}, {"get": {200: AuthPingResponseSerializer}}),
    "apps.identity.views.OrganizationMembersView": Contract(
        {"post": OrganizationInviteRequestSerializer},
        {
            "get": {200: OrganizationMembersResponseSerializer},
            "post": {201: OrganizationInviteResponseSerializer},
        },
    ),
    "apps.identity.views.OrganizationMemberDetailView": Contract(
        {"patch": OrganizationRoleRequestSerializer},
        {"patch": {200: OrganizationMemberSerializer}, "delete": {204: None}},
    ),
    "apps.identity.views.OrganizationInviteAcceptView": Contract(
        {"post": EmptyRequestSerializer}, {"post": {200: OrganizationMemberSerializer}}
    ),
    "apps.identity.settings_views.SettingsOrganizationView": Contract(
        {"patch": OrganizationSerializer},
        {
            "get": {200: OrganizationSerializer},
            "patch": {200: OrganizationSerializer},
        },
    ),
    "apps.identity.settings_views.SettingsConsentsView": Contract(
        {"post": ConsentRequestSerializer},
        {
            "get": {200: ConsentListResponseSerializer},
            "post": {200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT},
        },
    ),
    "apps.identity.settings_views.SettingsExportView": Contract(
        {"post": EmptyRequestSerializer}, {"post": {200: OpenApiTypes.OBJECT}}
    ),
    "apps.identity.settings_views.SettingsAccountView": Contract(
        {}, {"delete": {200: DetailResponseSerializer}}
    ),
    "apps.billing.views.StripeWebhookView": Contract(
        {"post": OpenApiTypes.OBJECT}, {"post": {200: StripeWebhookResponseSerializer}}
    ),
    "apps.billing.views.UsageView": Contract({}, {"get": {200: UsageResponseSerializer}}),
    "apps.governance.views.GovernanceDashboardView": Contract(
        {}, {"get": {200: DashboardResponseSerializer}}
    ),
    "apps.integrations.views.IncomingWebhookView": Contract(
        {"post": OpenApiTypes.OBJECT}, {"post": {200: IncomingWebhookResponseSerializer}}
    ),
}


class JTCodeAutoSchema(AutoSchema):
    """Supply typed contracts for deliberate APIViews without DRF serializer hooks."""

    def _contract(self) -> Contract | None:
        view_class = self.view.__class__
        return CONTRACTS.get(f"{view_class.__module__}.{view_class.__name__}")

    def get_request_serializer(self) -> Any:
        contract = self._contract()
        if contract and self.method.lower() in contract.request:
            serializer = contract.request[self.method.lower()]
            return (
                serializer()
                if isinstance(serializer, type) and issubclass(serializer, serializers.Serializer)
                else serializer
            )
        return super().get_request_serializer()

    def get_response_serializers(self) -> Any:
        contract = self._contract()
        if contract and self.method.lower() in contract.responses:
            return contract.responses[self.method.lower()]
        return super().get_response_serializers()
