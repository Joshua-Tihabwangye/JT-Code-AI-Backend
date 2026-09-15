from __future__ import annotations

from celery import shared_task
from django.utils import timezone


@shared_task
def cleanup_old_audit_events():
    """Clean up audit events older than retention period"""
    from apps.governance.models import AuditEvent, RetentionRule

    rules = RetentionRule.objects.filter(is_active=True)

    for rule in rules:
        cutoff = timezone.now() - timezone.timedelta(days=rule.retention_days + rule.grace_period_days)
        events = AuditEvent.objects.filter(
            organization=rule.organization,
            category=rule.data_category,
            created_at__lt=cutoff
        )

        if rule.action == RetentionRule.Action.HARD_DELETE:
            events.delete()
        elif rule.action == RetentionRule.Action.SOFT_DELETE:
            # Mark as deleted (would need a deleted_at field)
            pass
        elif rule.action == RetentionRule.Action.ANONYMIZE:
            # Anonymize sensitive fields
            pass


@shared_task
def cleanup_old_safety_events():
    """Clean up old safety events"""
    from apps.governance.models import SafetyEvent

    cutoff = timezone.now() - timezone.timedelta(days=2555)  # 7 years
    SafetyEvent.objects.filter(created_at__lt=cutoff).delete()


@shared_task
def check_consent_expiry():
    """Check for expiring consents"""
    from apps.governance.models import ConsentRecord

    from django.utils import timezone
    soon = timezone.now() + timezone.timedelta(days=30)
    expiring = ConsentRecord.objects.filter(
        status=ConsentRecord.Status.GRANTED,
        expires_at__lte=soon,
        expires_at__isnull=False
    )

    for consent in expiring:
        from apps.events.outbox import enqueue_outbox_event
        enqueue_outbox_event(
            topic='governance.consent.expiring',
            event_key=str(consent.id),
            payload={
                'consent_id': str(consent.id),
                'user_id': str(consent.user_id),
                'organization_id': str(consent.organization_id),
                'consent_type': consent.consent_type,
                'expires_at': consent.expires_at.isoformat(),
            },
            headers={'trace_id': f'consent-{consent.id}'}
        )