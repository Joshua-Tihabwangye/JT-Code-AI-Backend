from __future__ import annotations

import hashlib
import hmac
import json

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.identity.models import User


@csrf_exempt
def supabase_webhook(request: HttpRequest):
    if request.method != 'POST':
        return JsonResponse({'detail': 'Method not allowed.'}, status=405)
    if not settings.SUPABASE_WEBHOOK_SIGNING_SECRET:
        return JsonResponse({'detail': 'Webhook is not configured.'}, status=503)

    signature = request.headers.get('X-Supabase-Signature', '')
    expected = hmac.new(
        settings.SUPABASE_WEBHOOK_SIGNING_SECRET.encode(),
        request.body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return JsonResponse({'detail': 'Invalid webhook signature.'}, status=400)

    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'detail': 'Invalid JSON payload.'}, status=400)

    event_type = payload.get('type', '')
    record = payload.get('record', {})

    if event_type == 'DELETE' and record:
        supabase_id = record.get('id')
        if supabase_id:
            User.objects.filter(supabase_user_id=supabase_id).update(is_active=False)
        return JsonResponse({'received': True})

    if event_type in {'INSERT', 'UPDATE'} and record:
        supabase_id = record.get('id')
        if not supabase_id:
            return JsonResponse({'detail': 'Missing Supabase user id.'}, status=400)

        email = record.get('email', '')
        user_metadata = record.get('user_metadata', {}) or {}
        full_name = user_metadata.get('full_name', '') or user_metadata.get('name', '')
        display_name = full_name or ''
        avatar_url = user_metadata.get('avatar_url', '') or ''

        User.objects.update_or_create(supabase_user_id=supabase_id, defaults={
            'email': email,
            'full_name': full_name,
            'display_name': display_name,
            'avatar_url': avatar_url,
            'is_active': True,
        })
        return JsonResponse({'received': True})

    return JsonResponse({'received': True})
