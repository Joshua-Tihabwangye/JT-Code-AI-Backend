from __future__ import annotations

import hmac
import json

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.identity.models import User


def _authenticated(request: HttpRequest) -> bool:
    """Accept a signed request or the shared secret Supabase webhooks can send.

    Custom senders sign with the timestamp/nonce scheme of
    :mod:`apps.core.signing` (replay-protected). Supabase Database Webhooks
    (pg_net) cannot compute an HMAC, so they are configured with
    ``Authorization: Bearer <SUPABASE_WEBHOOK_SIGNING_SECRET>`` over TLS; every
    event they deliver is an idempotent state update.
    """
    from apps.core.signing import SIGNATURE_HEADER, SignatureError, verify_request

    secret = settings.SUPABASE_WEBHOOK_SIGNING_SECRET
    if request.headers.get(SIGNATURE_HEADER):
        try:
            verify_request(
                body=request.body,
                headers=request.headers,
                secrets_=[secret],
                namespace="supabase",
                tolerance_seconds=settings.WEBHOOK_REPLAY_TOLERANCE_SECONDS,
            )
        except SignatureError:
            return False
        return True
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return scheme.lower() == "bearer" and bool(token) and hmac.compare_digest(token, secret)


def _suspended(record: dict) -> bool:
    """Supabase marks bans with ``banned_until`` and soft deletes with ``deleted_at``."""
    if record.get("deleted_at"):
        return True
    banned_until = parse_datetime(str(record.get("banned_until") or ""))
    return banned_until is not None and banned_until > timezone.now()


@csrf_exempt
def supabase_webhook(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    if not settings.SUPABASE_WEBHOOK_SIGNING_SECRET:
        return JsonResponse({"detail": "Webhook is not configured."}, status=503)

    if not _authenticated(request):
        return JsonResponse({"detail": "Invalid webhook signature."}, status=401)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError, UnicodeDecodeError:
        return JsonResponse({"detail": "Invalid JSON payload."}, status=400)

    event_type = payload.get("type", "")
    record = payload.get("record", {})

    if event_type == "DELETE" and record:
        supabase_id = record.get("id")
        if supabase_id:
            User.objects.filter(supabase_user_id=supabase_id).update(is_active=False)
        return JsonResponse({"received": True})

    if event_type in {"INSERT", "UPDATE"} and record:
        supabase_id = record.get("id")
        if not supabase_id:
            return JsonResponse({"detail": "Missing Supabase user id."}, status=400)

        email = record.get("email", "")
        user_metadata = record.get("user_metadata", {}) or {}
        full_name = user_metadata.get("full_name", "") or user_metadata.get("name", "")
        display_name = full_name or ""
        avatar_url = user_metadata.get("avatar_url", "") or ""

        profile_defaults = {
            "email": email,
            "full_name": full_name,
            "display_name": display_name,
            "avatar_url": avatar_url,
        }
        if _suspended(record):
            User.objects.filter(supabase_user_id=supabase_id).update(is_active=False)
            return JsonResponse({"received": True})
        user, created = User.objects.get_or_create(
            supabase_user_id=supabase_id,
            defaults={**profile_defaults, "is_active": True},
        )
        if not created:
            # A local suspension is an authorization decision. A routine upstream
            # profile UPDATE must never silently reactivate that account.
            User.objects.filter(pk=user.pk).update(**profile_defaults)
        return JsonResponse({"received": True})

    return JsonResponse({"received": True})
