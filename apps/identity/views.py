from rest_framework.generics import RetrieveAPIView, RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.identity.serializers import UserProfileSerializer, UserSerializer


class MeView(RetrieveAPIView):
    serializer_class = UserSerializer
    def get_object(self):
        return self.request.user

class SettingsProfileView(RetrieveUpdateAPIView):
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user

class AuthPingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        user = request.user
        return Response({
            'authenticated': True,
            'userId': str(user.id),
            'supabaseUserId': user.supabase_user_id,
            'email': user.email,
        })