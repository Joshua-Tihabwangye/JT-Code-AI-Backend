"""Add credits to a user's workspace through the credit ledger (DEBUG only).

    python manage.py grant_local_credits you@example.com --credits 5000

For local development and the frontend's live integration suite, where Stripe
is not configured and the free plan's monthly credits run out quickly.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.billing.models import CreditLedger
from apps.billing.services import CreditService
from apps.identity.authorization import primary_organization_for_user
from apps.identity.models import User


class Command(BaseCommand):
    help = "Grant credits to a user's primary workspace (DEBUG only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("email")
        parser.add_argument("--credits", type=Decimal, default=Decimal("1000"))

    def handle(self, *args, **options) -> None:
        if not settings.DEBUG:
            raise CommandError("grant_local_credits only runs with DEBUG on (local development).")
        amount = options["credits"]
        if amount <= 0:
            raise CommandError("--credits must be positive.")
        user = User.objects.filter(email__iexact=options["email"].strip()).order_by("-date_joined").first()
        if user is None:
            raise CommandError("No user with that email has signed in yet.")
        organization = primary_organization_for_user(user)
        if organization is None:
            raise CommandError("The user has no workspace yet; sign in once first.")
        wallet = CreditService.get_or_create_wallet(organization)
        CreditService.add_credits(
            wallet,
            amount,
            reason="Local development credits",
            ledger_reason=CreditLedger.Reason.MANUAL_TOPUP,
            idempotency_key=f"local_grant_{uuid.uuid4()}",
        )
        wallet.refresh_from_db()
        self.stdout.write(self.style.SUCCESS(f"{organization.name}: balance {wallet.balance} credits."))
