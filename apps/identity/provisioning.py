"""First sign-in provisioning: profile from Supabase metadata and a personal workspace.

Supabase owns sign-up, so Django first sees a user on their first authenticated
request. Without an organization every tenant-scoped endpoint answers 403, so a
user who belongs to none gets a personal workspace (they are its owner, hence
admin), its credit wallet and the free plan's monthly credits.
"""

from __future__ import annotations

import secrets
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.identity.models import Organization, User, UserOrganization

_CHECKED_KEY = "jt-code:identity:workspace-checked:{user_id}"
_CHECK_TTL_SECONDS = 3600


def _metadata_text(metadata: dict[str, Any], *keys: str, limit: int) -> str:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:limit]
    return ""


def apply_signup_metadata(user: User, claims: dict[str, Any]) -> None:
    """Fill empty profile fields from the sign-up form data Supabase keeps in user_metadata."""
    metadata = claims.get("user_metadata")
    if not isinstance(metadata, dict):
        return
    values = {
        "first_name": _metadata_text(metadata, "first_name", "firstName", limit=150),
        "last_name": _metadata_text(metadata, "last_name", "lastName", limit=150),
        "full_name": _metadata_text(metadata, "full_name", "name", limit=255),
        "contact": _metadata_text(metadata, "contact", "phone", limit=100),
        "country": _metadata_text(metadata, "countryName", "country_name", limit=100),
        "country_code": _metadata_text(metadata, "country", "countryCode", limit=2).upper(),
        "dial_code": _metadata_text(metadata, "dialCode", "dial_code", limit=10),
        "timezone": _metadata_text(metadata, "timezone", limit=100),
    }
    if not values["full_name"]:
        values["full_name"] = " ".join(filter(None, [values["first_name"], values["last_name"]]))
    changed = [field for field, value in values.items() if value and not getattr(user, field)]
    for field in changed:
        setattr(user, field, values[field])
    if changed:
        user.save(update_fields=changed)


def ensure_personal_workspace(user: User) -> Organization | None:
    """Give a user without any organization a personal one. Returns it when created."""
    with transaction.atomic():
        # Serialize concurrent first requests of the same user.
        User.objects.select_for_update().filter(pk=user.pk).first()
        if UserOrganization.objects.filter(user=user).exists():
            return None
        label = user.first_name or user.full_name or (user.email.split("@")[0] if user.email else "My")
        name = f"{label}'s workspace"[:255]
        slug = f"{slugify(label)[:40] or 'workspace'}-{secrets.token_hex(4)}"
        organization = Organization.objects.create(name=name, slug=slug, owner=user)
        UserOrganization.objects.create(user=user, organization=organization)
        _grant_free_plan(organization)
    return organization


def _grant_free_plan(organization: Organization) -> None:
    from apps.billing.models import CreditLedger, Plan
    from apps.billing.services import CreditService

    wallet = CreditService.get_or_create_wallet(organization)
    plan = Plan.objects.filter(slug=settings.BILLING_DEFAULT_PLAN, status=Plan.Status.ACTIVE).first()
    if plan is None or not plan.is_free or plan.monthly_credits <= 0:
        return
    month = timezone.now().strftime("%Y-%m")
    # Same idempotency key as billing.tasks.grant_free_plan_credits: one grant per month.
    CreditService.add_credits(
        wallet,
        plan.monthly_credits,
        reason=f"{plan.name} plan credits for {month}",
        ledger_reason=CreditLedger.Reason.SUBSCRIPTION_GRANT,
        idempotency_key=f"free_grant_{organization.id}_{month}",
    )


def provision_on_authentication(user: User, claims: dict[str, Any], *, created: bool) -> None:
    """Called by Supabase authentication; cheap after the first request (cached check)."""
    key = _CHECKED_KEY.format(user_id=user.pk)
    if not created and cache.get(key):
        return
    if created:
        apply_signup_metadata(user, claims)
    ensure_personal_workspace(user)
    cache.set(key, True, _CHECK_TTL_SECONDS)
