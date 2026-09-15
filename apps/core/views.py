from __future__ import annotations

import secrets
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
import sentry_sdk

class LiveView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    def get(self, request: Request) -> Response:
        return Response({'status': 'ok', 'service': 'jt-code-api'})

class ReadyView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    def get(self, request: Request) -> Response:
        checks: dict[str, str] = {}
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
            checks['database'] = 'ok'
        except Exception:
            checks['database'] = 'failed'
        try:
            cache.set('healthcheck', 'ok', timeout=5)
            checks['redis'] = 'ok' if cache.get('healthcheck') == 'ok' else 'failed'
        except Exception:
            checks['redis'] = 'failed'
        ready = all(value == 'ok' for value in checks.values())
        return Response({'status': 'ok' if ready else 'degraded', 'checks': checks}, status=200 if ready else 503)

class N8nSentryRelayView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    def post(self, request: Request) -> Response:
        expected = settings.N8N_SENTRY_RELAY_SECRET
        supplied = request.headers.get('X-JT-Code-Relay-Secret', '')
        if not expected or not secrets.compare_digest(expected, supplied):
            return Response({'detail': 'Unauthorized relay request.'}, status=status.HTTP_401_UNAUTHORIZED)
        payload = request.data if isinstance(request.data, dict) else {}
        with sentry_sdk.push_scope() as scope:
            scope.set_tag('source', 'n8n')
            scope.set_context('n8n', {k: payload.get(k) for k in ('workflowId', 'executionId', 'step', 'errorCode')})
            sentry_sdk.capture_message(str(payload.get('message', 'n8n workflow failure')), level='error')
        return Response(status=status.HTTP_202_ACCEPTED)
