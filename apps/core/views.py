from __future__ import annotations

import logging
import secrets
from collections.abc import Callable

import sentry_sdk
from celery import current_app
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


def _check(name: str, operation: Callable[[], None]) -> str:
    try:
        operation()
    except Exception:
        logger.warning("readiness dependency failed", extra={"dependency": name}, exc_info=True)
        return "failed"
    return "ok"


class LiveView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type[BaseAuthentication]] = []

    def get(self, request: Request) -> Response:
        logger.info("liveness probe completed")
        return Response({"status": "ok", "service": "jt-code-api"})


class StartupView(LiveView):
    """A startup probe: reaching this view proves Django settings and URL loading succeeded."""


class ReadyView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type[BaseAuthentication]] = []

    def get(self, request: Request) -> Response:
        def database() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

        def redis() -> None:
            cache.set("healthcheck", "ok", timeout=5)
            if cache.get("healthcheck") != "ok":
                raise RuntimeError("cache round trip failed")

        checks = {"database": _check("database", database), "redis": _check("redis", redis)}
        if settings.HEALTHCHECK_EXTERNAL_DEPENDENCIES:
            checks["celery_broker"] = _check(
                "celery_broker", lambda: current_app.connection_for_read().ensure_connection(max_retries=0)
            )

            def kafka() -> None:
                from confluent_kafka.admin import AdminClient

                from apps.events.kafka import kafka_client_config

                AdminClient(kafka_client_config()).list_topics(timeout=2)

            checks["kafka"] = _check("kafka", kafka)
        ready = all(value == "ok" for value in checks.values())
        logger.info("readiness probe completed", extra={"ready": ready, "checks": checks})
        return Response(
            {"status": "ok" if ready else "degraded", "checks": checks}, status=200 if ready else 503
        )


class N8nSentryRelayView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type[BaseAuthentication]] = []

    def post(self, request: Request) -> Response:
        expected = settings.N8N_SENTRY_RELAY_SECRET
        supplied = request.headers.get("X-JT-Code-Relay-Secret", "")
        if not expected or not secrets.compare_digest(expected, supplied):
            logger.warning("rejected n8n sentry relay request")
            return Response({"detail": "Unauthorized relay request."}, status=status.HTTP_401_UNAUTHORIZED)
        payload = request.data if isinstance(request.data, dict) else {}
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("source", "n8n")
            scope.set_context(
                "n8n", {k: payload.get(k) for k in ("workflowId", "executionId", "step", "errorCode")}
            )
            sentry_sdk.capture_message(str(payload.get("message", "n8n workflow failure")), level="error")
        logger.error("n8n reported workflow failure", extra={"workflow_id": payload.get("workflowId")})
        return Response(status=status.HTTP_202_ACCEPTED)
