from __future__ import annotations

from celery import shared_task
from django.utils import timezone


@shared_task
def process_webhooks():
    """Process pending Stripe webhooks"""
    # This would process any queued webhook events
    pass


@shared_task
def grant_monthly_credits():
    """Grant monthly credits to active subscriptions"""
    from apps.billing.models import Subscription
    from apps.billing.services import CreditService

    subscriptions = Subscription.objects.filter(
        status__in=[Subscription.Status.ACTIVE, Subscription.Status.TRIALING]
    ).select_related('plan', 'organization')

    for sub in subscriptions:
        # Check if credits already granted for this month
        from apps.billing.models import CreditLedger
        current_month = timezone.now().month
        already_granted = CreditLedger.objects.filter(
            wallet__organization=sub.organization,
            reason=CreditLedger.Reason.SUBSCRIPTION_GRANT,
            created_at__month=current_month
        ).exists()

        if not already_granted:
            CreditService.grant_subscription_credits(sub)


@shared_task
def check_subscription_renewals():
    """Check for subscriptions that need renewal"""
    from apps.billing.models import Subscription

    # Subscriptions expiring in 7 days
    from django.utils import timezone
    soon = timezone.now() + timezone.timedelta(days=7)
    expiring = Subscription.objects.filter(
        status=Subscription.Status.ACTIVE,
        current_period_end__lte=soon
    )

    for sub in expiring:
        # Send notification
        from apps.events.outbox import enqueue_outbox_event
        enqueue_outbox_event(
            topic='billing.subscription.renewing_soon',
            event_key=str(sub.id),
            payload={
                'subscription_id': str(sub.id),
                'organization_id': str(sub.organization_id),
                'plan_name': sub.plan.name,
                'days_until_renewal': (sub.current_period_end - timezone.now()).days,
            },
            headers={'trace_id': f'sub-renewal-{sub.id}'}
        )


@shared_task
def reconcile_usage():
    """Reconcile usage with provider invoices"""
    # This would compare model_run costs with Stripe invoices
    pass