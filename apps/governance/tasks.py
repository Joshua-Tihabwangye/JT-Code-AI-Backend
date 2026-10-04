from __future__ import annotations

from celery import shared_task
from django.utils import timezone


def _allow_audit_purge() -> None:
    """The append-only trigger permits retention deletes only inside this transaction."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('jt_code.ledger_purge', 'on', true)")


@shared_task
def cleanup_old_audit_events() -> dict[str, int]:
    """Apply audit retention: tenant ``audit_events`` rules, else ``AUDIT_EVENT_RETENTION_DAYS``.

    A rule under legal hold keeps everything. ``anonymize`` strips the actor,
    IP address and user agent but keeps the event; ``archive`` and
    ``soft_delete`` keep events in place (the Kafka export is the archive).
    """
    from django.conf import settings
    from django.db import transaction

    from apps.governance.models import AuditEvent, RetentionRule

    now = timezone.now()
    deleted = anonymized = 0
    rules = RetentionRule.objects.filter(
        is_active=True, data_category=RetentionRule.DataCategory.AUDIT_EVENTS
    )
    ruled_orgs = []
    for rule in rules:
        ruled_orgs.append(rule.organization_id)
        if rule.legal_hold:
            continue
        cutoff = now - timezone.timedelta(days=rule.retention_days + rule.grace_period_days)
        events = AuditEvent.objects.filter(organization_id=rule.organization_id, created_at__lt=cutoff)
        with transaction.atomic():
            _allow_audit_purge()
            if rule.action == RetentionRule.Action.HARD_DELETE:
                deleted += events.delete()[0]
            elif rule.action == RetentionRule.Action.ANONYMIZE:
                anonymized += events.exclude(actor=None, ip_address=None, user_agent="").update(
                    actor=None, ip_address=None, user_agent=""
                )
    default_cutoff = now - timezone.timedelta(days=settings.AUDIT_EVENT_RETENTION_DAYS)
    with transaction.atomic():
        _allow_audit_purge()
        deleted += (
            AuditEvent.objects.filter(created_at__lt=default_cutoff)
            .exclude(organization_id__in=ruled_orgs)
            .delete()[0]
        )
    return {"deleted": deleted, "anonymized": anonymized}


@shared_task
def cleanup_old_safety_events():
    """Clean up old safety events"""
    from django.conf import settings

    from apps.governance.models import SafetyEvent

    cutoff = timezone.now() - timezone.timedelta(days=settings.SAFETY_EVENT_RETENTION_DAYS)
    SafetyEvent.objects.filter(created_at__lt=cutoff).delete()


@shared_task
def check_consent_expiry():
    """Check for expiring consents"""
    from django.utils import timezone

    from apps.governance.models import ConsentRecord

    soon = timezone.now() + timezone.timedelta(days=30)
    expiring = ConsentRecord.objects.filter(
        status=ConsentRecord.Status.GRANTED, expires_at__lte=soon, expires_at__isnull=False
    )

    for consent in expiring:
        from apps.events.outbox import enqueue_outbox_event

        enqueue_outbox_event(
            topic="governance.consent.expiring",
            event_key=str(consent.id),
            payload={
                "consent_id": str(consent.id),
                "user_id": str(consent.user_id),
                "organization_id": str(consent.organization_id),
                "consent_type": consent.consent_type,
                "expires_at": consent.expires_at.isoformat(),
            },
            headers={"trace_id": f"consent-{consent.id}"},
        )
