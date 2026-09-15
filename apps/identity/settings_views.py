from __future__ import annotations

import json

from django.http import HttpResponse
from django.utils.timezone import now
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.governance.models import ConsentRecord
from apps.governance.serializers import ConsentRecordSerializer
from apps.identity.models import Organization, UserOrganization
from apps.identity.serializers import OrganizationSerializer


def _get_org(user) -> Organization | None:
    return user.organizations.first()


class SettingsOrganizationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        org = _get_org(request.user)
        if not org:
            return Response({'detail': 'No organization found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(OrganizationSerializer(org).data)

    def patch(self, request: Request) -> Response:
        org = _get_org(request.user)
        if not org:
            return Response({'detail': 'No organization found'}, status=status.HTTP_404_NOT_FOUND)
        serializer = OrganizationSerializer(org, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class SettingsConsentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        org = _get_org(request.user)
        consents = ConsentRecord.objects.filter(user=request.user)
        if org:
            consents = consents.filter(organization=org)
        consents = consents.select_related('organization', 'user').order_by('consent_type')
        return Response({'results': ConsentRecordSerializer(consents, many=True).data})

    def post(self, request: Request) -> Response:
        consent_type = request.data.get('consent_type')
        requested_status = request.data.get('status')
        if consent_type not in ConsentRecord.ConsentType.values:
            return Response({'detail': 'Invalid consent type'}, status=status.HTTP_400_BAD_REQUEST)
        if requested_status not in ('granted', 'denied'):
            return Response({'detail': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_org(request.user)
        if not org:
            return Response({'detail': 'No organization found'}, status=status.HTTP_404_NOT_FOUND)

        consent, created = ConsentRecord.objects.get_or_create(
            user=request.user,
            organization=org,
            consent_type=consent_type,
            defaults={
                'status': requested_status,
                'version': '1.0',
                'granted_at': None,
                'ip_address': request.META.get('REMOTE_ADDR', ''),
                'user_agent': request.META.get('HTTP_USER_AGENT', '')[:500],
            },
        )
        if not created:
            consent.status = requested_status
            consent.version = '1.0'
            consent.save(update_fields=['status', 'version', 'updated_at'])

        serializer = ConsentRecordSerializer(consent)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SettingsExportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        user = request.user
        from apps.assets.models import Asset
        from apps.conversations.models import ChatRequest, Conversation

        export = {
            'user': {
                'id': str(user.id),
                'supabase_user_id': user.supabase_user_id,
                'email': user.email,
                'full_name': user.full_name,
                'display_name': user.display_name,
                'job_title': user.job_title,
                'contact': user.contact,
                'country': user.country,
                'timezone': user.timezone,
                'bio': user.bio,
            },
            'organizations': [
                {
                    'id': str(org.id),
                    'name': org.name,
                    'slug': org.slug,
                    'timezone': org.timezone,
                }
                for org in user.organizations.all()
            ],
            'conversations': [
                {
                    'id': str(c.id),
                    'title': c.title,
                    'created_at': c.created_at.isoformat(),
                }
                for c in Conversation.objects.filter(owner=user)
            ],
            'chat_requests': [
                {
                    'id': str(r.id),
                    'conversation_id': str(r.conversation_id),
                    'task_type': r.task_type,
                    'status': r.status,
                    'input_text': r.input_text,
                    'output_text': r.output_text,
                    'created_at': r.created_at.isoformat(),
                }
                for r in ChatRequest.objects.filter(owner=user)
            ],
            'assets': [
                {
                    'id': str(a.id),
                    'cloudinary_public_id': a.cloudinary_public_id,
                    'secure_url': a.secure_url,
                    'resource_type': a.resource_type,
                    'format': a.format,
                    'bytes': a.bytes,
                    'created_at': a.created_at.isoformat(),
                }
                for a in Asset.objects.filter(owner=user)
            ],
            'consents': [
                {
                    'consent_type': c.consent_type,
                    'status': c.status,
                    'version': c.version,
                    'granted_at': c.granted_at.isoformat() if c.granted_at else None,
                }
                for c in ConsentRecord.objects.filter(user=user)
            ],
            'exported_at': now().isoformat(),
        }

        payload = json.dumps(export, indent=2, ensure_ascii=False)
        response = HttpResponse(payload, content_type='application/json')
        response['Content-Disposition'] = 'attachment; filename="jt-code-export.json"'
        return response


class SettingsAccountView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request: Request) -> Response:
        user = request.user
        UserOrganization.objects.filter(user=user).delete()
        user.is_active = False
        user.email = ''
        user.full_name = ''
        user.display_name = ''
        user.first_name = ''
        user.last_name = ''
        user.contact = ''
        user.bio = ''
        user.avatar_url = ''
        user.username = None
        user.save(update_fields=[
            'is_active', 'email', 'full_name', 'display_name', 'first_name',
            'last_name', 'contact', 'bio', 'avatar_url', 'username', 'updated_at',
        ])
        return Response({'detail': 'Account deleted'}, status=status.HTTP_200_OK)