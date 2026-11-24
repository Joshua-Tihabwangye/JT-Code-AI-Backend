"""Support cases: customers write in the app, JT-Code support answers in the Django admin.

Notifications go out by e-mail from a Celery task (a mail problem never fails the request):

* a new case, or a customer reply, to ``SUPPORT_NOTIFICATION_EMAIL`` (nothing is sent when it is empty);
* a support reply to the customer who opened the case.

The mails carry the subject and the text; the case link points staff at the Django admin.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction

logger = logging.getLogger(__name__)


def _case(case_id: str):
    from apps.governance.models import SupportCase

    return SupportCase.objects.select_related("user", "organization").filter(id=case_id).first()


@shared_task(autoretry_for=(OSError,), retry_backoff=60, max_retries=5, ignore_result=True)
def notify_support(case_id: str, message_id: str | None = None) -> bool:
    recipient = settings.SUPPORT_NOTIFICATION_EMAIL
    case = _case(case_id)
    if not recipient or case is None:
        return False
    from apps.governance.models import SupportCaseMessage

    message = SupportCaseMessage.objects.filter(id=message_id).first() if message_id else None
    kind = "Reply" if message else "New case"
    body = message.body if message else case.description
    send_mail(
        subject=f"[JT-Code support] {kind}: {case.subject}",
        message=(
            f"{kind} from {case.user.email} ({case.organization.name})\n"
            f"Category: {case.category} · Priority: {case.priority} · Case: {case.id}\n\n{body}\n\n"
            "Answer in the Django admin (Governance → Support cases)."
        ),
        from_email=None,
        recipient_list=[recipient],
    )
    return True


@shared_task(autoretry_for=(OSError,), retry_backoff=60, max_retries=5, ignore_result=True)
def notify_customer(case_id: str, message_id: str) -> bool:
    from apps.governance.models import SupportCaseMessage

    case = _case(case_id)
    message = SupportCaseMessage.objects.filter(id=message_id).first()
    if case is None or message is None or not case.user.email:
        return False
    link = f"{settings.FRONTEND_URL.rstrip('/')}/app/settings?tab=support" if settings.FRONTEND_URL else ""
    send_mail(
        subject=f"Re: {case.subject}",
        message=f"JT-Code support replied to your case:\n\n{message.body}\n\n{link}".rstrip() + "\n",
        from_email=None,
        recipient_list=[case.user.email],
    )
    return True


def after_commit(task, *args) -> None:
    def send() -> None:
        try:
            task.delay(*args)
        except Exception:  # noqa: BLE001 - the case and message are saved; mail is best effort
            logger.warning("support notification could not be queued", exc_info=True)

    transaction.on_commit(send)


def add_message(case, author, role: str, body: str):
    """Store a reply, move the case on, and notify the other side (after commit)."""
    from django.utils import timezone

    from apps.governance.models import SupportCase, SupportCaseMessage

    message = SupportCaseMessage.objects.create(case=case, author=author, author_role=role, body=body)
    updates = ["status", "updated_at"]
    if role == SupportCaseMessage.Author.SUPPORT:
        case.status = SupportCase.Status.WAITING_USER
        if not case.first_response_at:
            case.first_response_at = timezone.now()
            updates.append("first_response_at")
        after_commit(notify_customer, str(case.id), str(message.id))
    else:
        # A customer reply reopens a resolved case and puts it back in the support queue.
        case.status = SupportCase.Status.OPEN
        case.resolved_at = None
        updates.append("resolved_at")
        after_commit(notify_support, str(case.id), str(message.id))
    case.save(update_fields=updates)
    return message
