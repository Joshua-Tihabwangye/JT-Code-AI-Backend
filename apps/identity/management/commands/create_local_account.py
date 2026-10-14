"""Create a ready-to-use local login: Supabase Auth user, organization membership and credits.

    python manage.py create_local_account you@example.com --org "My workspace"

The password is prompted for (never echoed or passed on the command line). The
same login works in the frontend and the API. Development only: the
command refuses to run unless DEBUG is on.
"""

from __future__ import annotations

import getpass
import secrets
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils.text import slugify

from apps.billing.models import CreditLedger
from apps.billing.services import CreditService
from apps.identity.models import Organization, User, UserOrganization
from apps.identity.supabase_admin import SupabaseAdminError, create_auth_user


class Command(BaseCommand):
    help = "Create a local Supabase login with an organization and credits (DEBUG only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("email")
        parser.add_argument(
            "--org", default="", help="Organization name (default: '<email name>'s workspace')."
        )
        parser.add_argument("--credits", type=Decimal, default=Decimal("1000"), help="Credits to grant once.")

    def handle(self, *args, **options) -> None:
        if not settings.DEBUG:
            raise CommandError("create_local_account only runs with DEBUG on (local development).")
        email = options["email"].strip().lower()
        password = getpass.getpass("Password (8+ characters): ")
        if len(password) < 8 or password != getpass.getpass("Repeat password: "):
            raise CommandError("Passwords must match and have at least 8 characters.")

        try:
            supabase_user_id, created = create_auth_user(email, password)
        except (SupabaseAdminError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        if not created:
            self.stdout.write(f"{email} already exists in Supabase Auth; its password was left unchanged.")

        name = options["org"] or f"{email.split('@')[0]}'s workspace"
        with transaction.atomic():
            user, _ = User.objects.get_or_create(supabase_user_id=supabase_user_id, defaults={"email": email})
            organization = Organization.objects.filter(owner=user, name=name).first()
            if organization is None:
                slug = f"{slugify(name)[:40] or 'workspace'}-{secrets.token_hex(3)}"
                organization = Organization.objects.create(name=name, slug=slug, owner=user)
            UserOrganization.objects.get_or_create(user=user, organization=organization)
            wallet = CreditService.get_or_create_wallet(organization)
            key = f"local_account_grant_{organization.id}"
            if (
                options["credits"] > 0
                and not CreditLedger.objects.filter(wallet=wallet, idempotency_key=key).exists()
            ):
                CreditService.add_credits(
                    wallet,
                    options["credits"],
                    reason="Local development credits",
                    ledger_reason=CreditLedger.Reason.MANUAL_TOPUP,
                    idempotency_key=key,
                )
        self.stdout.write(
            self.style.SUCCESS(f"Ready: sign in as {email} (organization '{organization.name}').")
        )
