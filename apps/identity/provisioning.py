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


def _http_url(value: str) -> str:
    return value if value.startswith(("https://", "http://")) and len(value) <= 200 else ""


def profile_values_from_metadata(metadata: Any) -> dict[str, str]:
    """Profile fields carried by Supabase ``user_metadata`` (sign-up form data or an OAuth profile).

    The sign-up page sends ``first_name``/``last_name``/``contact``/``country``…; OAuth providers send
    ``full_name``/``name`` and ``avatar_url``/``picture``. Unknown or non-text values are ignored.
    """
    if not isinstance(metadata, dict):
        return {}
    values = {
        "first_name": _metadata_text(metadata, "first_name", "firstName", "given_name", limit=150),
        "last_name": _metadata_text(metadata, "last_name", "lastName", "family_name", limit=150),
        "full_name": _metadata_text(metadata, "full_name", "name", limit=255),
        "contact": _metadata_text(metadata, "contact", "phone", limit=100),
        "country": _metadata_text(metadata, "countryName", "country_name", limit=100),
        "country_code": _metadata_text(metadata, "country", "countryCode", limit=2).upper(),
        "dial_code": _metadata_text(metadata, "dialCode", "dial_code", limit=8),
        "timezone": _metadata_text(metadata, "timezone", limit=100),
        "avatar_url": _http_url(_metadata_text(metadata, "avatar_url", "picture", limit=200)),
    }
    if not values["full_name"]:
        values["full_name"] = " ".join(filter(None, [values["first_name"], values["last_name"]]))
    if values["full_name"] and not (values["first_name"] or values["last_name"]):
        first, _, last = values["full_name"].partition(" ")
        values["first_name"], values["last_name"] = first[:150], last.strip()[:150]
    if not values["country_code"].isalpha():
        values["country_code"] = ""
    return {field: value for field, value in values.items() if value}


def fill_empty_profile(user: User, metadata: Any) -> list[str]:
    """Copy Supabase metadata into the user's *empty* profile fields; return the fields written.

    Never overwrites: once a field has a value (from sign-up or an edit in Settings), Django owns it.
    """
    values = profile_values_from_metadata(metadata)
    if values.get("avatar_url") and user.avatar_asset_id:
        values.pop("avatar_url")
    changed = [field for field, value in values.items() if not getattr(user, field)]
    for field in changed:
        setattr(user, field, values[field])
    if changed:
        user.save(update_fields=[*changed, "updated_at"])
    return changed


def apply_signup_metadata(user: User, claims: dict[str, Any]) -> None:
    """Fill empty profile fields from the sign-up form data Supabase keeps in user_metadata."""
    fill_empty_profile(user, claims.get("user_metadata"))


def record_signup_consents(
    user: User,
    organization: Organization | None,
    claims: dict[str, Any],
    *,
    ip: str | None = None,
    user_agent: str = "",
) -> int:
    """Record the terms/privacy acceptance the sign-up form collected (``terms_accepted`` in metadata).

    Idempotent: a consent of the same type that is already recorded (any status) is left alone, so a
    later withdrawal in Settings is never re-granted by an old sign-up flag.
    """
    from apps.governance.models import ConsentRecord

    metadata = claims.get("user_metadata")
    if organization is None or not isinstance(metadata, dict):
        return 0
    wanted = [
        consent_type
        for consent_type, flag in (
            (ConsentRecord.ConsentType.TERMS, "terms_accepted"),
            (ConsentRecord.ConsentType.PRIVACY, "privacy_accepted"),
        )
        if metadata.get(flag) is True
    ]
    if not wanted:
        return 0
    existing = set(
        ConsentRecord.objects.filter(user=user, consent_type__in=wanted).values_list(
            "consent_type", flat=True
        )
    )
    version = _metadata_text(metadata, "consent_version", limit=50) or settings.CONSENT_VERSION
    now = timezone.now()
    created = 0
    for consent_type in wanted:
        if consent_type in existing:
            continue
        ConsentRecord.objects.create(
            user=user,
            organization=organization,
            consent_type=consent_type,
            status=ConsentRecord.Status.GRANTED,
            version=version,
            granted_at=now,
            ip_address=ip,
            user_agent=user_agent[:500],
            metadata={"source": "signup"},
        )
        created += 1
    return created


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


def provision_on_authentication(
    user: User,
    claims: dict[str, Any],
    *,
    created: bool,
    ip: str | None = None,
    user_agent: str = "",
) -> None:
    """Called by Supabase authentication; cheap after the first request (cached check).

    Runs once per user per ``_CHECK_TTL_SECONDS`` window, not only when the row is created: a row made
    by an admin command or the user webhook (e-mail only) is completed from the session's metadata on
    the next request, without ever overwriting a field the user has edited.
    """
    key = _CHECKED_KEY.format(user_id=user.pk)
    if not created and cache.get(key):
        return
    apply_signup_metadata(user, claims)
    organization = ensure_personal_workspace(user)
    if organization is None:
        organization = user.organizations.order_by("user_memberships__created_at").first()
    record_signup_consents(user, organization, claims, ip=ip, user_agent=user_agent)
    cache.set(key, True, _CHECK_TTL_SECONDS)
