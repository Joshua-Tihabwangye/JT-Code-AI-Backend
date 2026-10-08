from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.ai_gateway.image_api import (
    ImageEditJsonView,
    ImageGenerateView,
    ImageGenerationViewSet,
    ImageModelsView,
)
from apps.ai_gateway.image_views import (
    ImageEditView,
    ImageGenerationView,
    ImageUnderstandingView,
    generated_image_download,
)
from apps.ai_gateway.views import (
    AIModelsView,
    CompletionView,
    EmbeddingView,
    EvaluationViewSet,
    ModelAliasViewSet,
    ModelPolicyViewSet,
    ModelRunViewSet,
    ModelViewSet,
    PromptViewSet,
    ProviderViewSet,
    SystemCapabilitiesView,
)

router = DefaultRouter()
router.register(r"images", ImageGenerationViewSet, basename="image-generation-item")
router.register(r"providers", ProviderViewSet, basename="provider")
router.register(r"models", ModelViewSet, basename="model")
router.register(r"policies", ModelPolicyViewSet, basename="policy")
router.register(r"model-aliases", ModelAliasViewSet, basename="model-alias")
router.register(r"runs", ModelRunViewSet, basename="run")
router.register(r"prompts", PromptViewSet, basename="prompt")
router.register(r"evaluations", EvaluationViewSet, basename="evaluation")

urlpatterns = [
    path("completion/", CompletionView.as_view(), name="completion"),
    path("system/capabilities/", SystemCapabilitiesView.as_view(), name="system-capabilities"),
    path("embeddings/", EmbeddingView.as_view(), name="embeddings"),
    path("available-models/", AIModelsView.as_view(), name="available-models"),
    # Frontend image contract (generations gallery, JSON generate/edit).
    path("images/models/", ImageModelsView.as_view(), name="image-models"),
    path("images/generate/", ImageGenerateView.as_view(), name="image-generate"),
    path("images/edit/", ImageEditJsonView.as_view(), name="image-edit-json"),
    path("images/generations/", ImageGenerationView.as_view(), name="image-generation"),
    path("images/edits/", ImageEditView.as_view(), name="image-edit"),
    path("images/understand/", ImageUnderstandingView.as_view(), name="image-understand"),
    path("images/<uuid:id>/download/", generated_image_download, name="generated-image-download"),
    path("", include(router.urls)),
]
