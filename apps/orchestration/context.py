"""Minimal, purpose-bound context added to event deliveries for notification workflows.

n8n receives only what the receiving workflow needs: customer-facing events
get the organization's name and the e-mail addresses of its owner/admins plus a
ready-to-send subject and summary; operations events get a summary only. No
other personal data leaves Django.
"""

from __future__ import annotations

from typing import Any

_CUSTOMER_EVENTS = {
    "billing.subscription.renewing_soon": (
        "Your JT-Code subscription renews soon",
        "Your {organization} subscription renews on {renews_at}.",
    ),
    "governance.consent.expiring": (
        "A JT-Code consent is about to expire",
        "A consent recorded for {organization} expires soon. Review it in Settings > Privacy.",
    ),
    "jobs.job.failed": (
        "A JT-Code job failed",
        "Job {job_id} for {organization} failed: {error}",
    ),
}
_OPS_EVENTS = {
    "knowledge.document.index_failed": "Document {document_id} failed to index: {error}",
    "events.dead_lettered": "Consumer {consumer_group} dead-lettered {event_type}: {error}",
    "safety.image_prompt_blocked": "An image prompt was blocked for organization {organization_id}.",
    "knowledge.integration.sync_requested": "Integration sync requested for source {sourceId}.",
}


class _Defaults(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "-"


def _recipients(organization: Any) -> list[str]:
    from apps.identity.models import Role, UserRole

    emails = set()
    if getattr(organization, "owner", None) is not None and organization.owner.email:
        emails.add(organization.owner.email)
    admins = UserRole.objects.filter(
        organization=organization, role__name=Role.RoleType.ADMIN, user__is_active=True
    ).values_list("user__email", flat=True)
    emails.update(email for email in admins if email)
    return sorted(emails)[:20]


def enrich(event_type: str, payload: dict[str, Any], organization: Any = None) -> dict[str, Any]:
    values = _Defaults({k: str(v)[:300] for k, v in payload.items() if not isinstance(v, dict | list)})
    if organization is not None:
        values["organization"] = organization.name
    if event_type in _CUSTOMER_EVENTS and organization is not None:
        subject, summary = _CUSTOMER_EVENTS[event_type]
        return {
            "audience": "customer",
            "organizationName": organization.name,
            "recipients": _recipients(organization),
            "subject": subject,
            "summary": summary.format_map(values),
        }
    template = _OPS_EVENTS.get(event_type, "{event_type} occurred.")
    values.setdefault("event_type", event_type)
    return {"audience": "operations", "summary": template.format_map(values)}
