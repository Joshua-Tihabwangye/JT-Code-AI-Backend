from rest_framework.routers import DefaultRouter

from apps.agents.views import AgentDefinitionViewSet, AgentRunViewSet

router = DefaultRouter()
router.register("agents", AgentDefinitionViewSet, basename="agent")
router.register("agent-runs", AgentRunViewSet, basename="agent-run")
urlpatterns = router.urls
