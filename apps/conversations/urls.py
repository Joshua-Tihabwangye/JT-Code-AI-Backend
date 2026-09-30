from rest_framework.routers import DefaultRouter

from apps.conversations.runtime_views import ChatRequestRuntimeViewSet, ConversationRuntimeViewSet

router = DefaultRouter()
router.register("conversations", ConversationRuntimeViewSet, basename="conversation")
router.register("chat/requests", ChatRequestRuntimeViewSet, basename="chat-request")
urlpatterns = router.urls
