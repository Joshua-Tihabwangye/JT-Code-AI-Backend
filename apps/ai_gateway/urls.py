from django.urls import include, path
from rest_framework.routers import DefaultRouter

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
    ModelPolicyViewSet,
    ModelRunViewSet,
    ModelViewSet,
    PromptViewSet,
    ProviderViewSet,
)

router = DefaultRouter()
router.register(r'providers', ProviderViewSet, basename='provider')
router.register(r'models', ModelViewSet, basename='model')
router.register(r'policies', ModelPolicyViewSet, basename='policy')
router.register(r'runs', ModelRunViewSet, basename='run')
router.register(r'prompts', PromptViewSet, basename='prompt')
router.register(r'evaluations', EvaluationViewSet, basename='evaluation')

urlpatterns = [
    path('', include(router.urls)),
    path('completion/', CompletionView.as_view(), name='completion'),
    path('embeddings/', EmbeddingView.as_view(), name='embeddings'),
    path('available-models/', AIModelsView.as_view(), name='available-models'),
    path('images/generations/', ImageGenerationView.as_view(), name='image-generation'),
    path('images/edits/', ImageEditView.as_view(), name='image-edit'),
    path('images/understand/', ImageUnderstandingView.as_view(), name='image-understand'),
    path('images/<uuid:id>/download/', generated_image_download, name='generated-image-download'),
]