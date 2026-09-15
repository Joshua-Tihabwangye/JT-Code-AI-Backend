from rest_framework.routers import DefaultRouter
from apps.conversations.views import ChatRequestViewSet, ConversationViewSet

router = DefaultRouter()
router.register('conversations', ConversationViewSet, basename='conversation')
router.register('chat/requests', ChatRequestViewSet, basename='chat-request')
urlpatterns = router.urls
