"""Create deterministic, non-sensitive canonical rows for an isolated restore drill."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Seed idempotent canonical rows so a restore drill proves data restoration, not just schema restore."
    )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ARG002
        from apps.assets.models import Asset
        from apps.billing.models import CreditLedger, CreditWallet
        from apps.conversations.models import Conversation
        from apps.governance.models import AuditEvent
        from apps.identity.models import Organization, User
        from apps.jobs.models import Job

        user, _ = User.objects.get_or_create(
            supabase_user_id="restore-drill-user",
            defaults={"username": "restore-drill-user", "email": "restore-drill@example.invalid"},
        )
        organization, _ = Organization.objects.get_or_create(
            slug="restore-drill",
            defaults={"name": "Restore drill", "owner": user},
        )
        if organization.owner_id is None:
            organization.owner = user
            organization.save(update_fields=["owner", "updated_at"])
        user.organizations.add(organization)

        conversation, _ = Conversation.objects.get_or_create(
            owner=user, organization=organization, title="Restore drill conversation"
        )
        job, _ = Job.objects.get_or_create(
            owner=user,
            organization=organization,
            idempotency_key="restore-drill-job",
            defaults={
                "task_type": Job.TaskType.GENERAL_QUESTION,
                "input_payload": {"fixture": "restore-drill"},
                "trace_id": "restore-drill",
                "conversation": conversation,
            },
        )
        Asset.objects.get_or_create(
            imagekit_file_id="restore-drill-asset",
            defaults={
                "owner": user,
                "organization": organization,
                "imagekit_file_path": "/restore-drill/asset.txt",
                "secure_url": "https://example.invalid/restore-drill/asset.txt",
                "resource_type": "file",
                "original_filename": "restore-drill.txt",
            },
        )
        wallet, _ = CreditWallet.objects.get_or_create(
            organization=organization,
            defaults={"balance": Decimal("1"), "reserved_balance": Decimal("0")},
        )
        CreditLedger.objects.get_or_create(
            wallet=wallet,
            idempotency_key="restore-drill-ledger",
            defaults={
                "direction": CreditLedger.Direction.CREDIT,
                "credits": Decimal("1"),
                "reason": CreditLedger.Reason.ADJUSTMENT,
                "description": "Restore drill fixture",
                "balance_after": wallet.balance,
            },
        )
        AuditEvent.objects.get_or_create(
            organization=organization,
            actor=user,
            action="restore_drill.seeded",
            resource_type="restore_drill",
            resource_id=str(job.id),
            defaults={
                "category": AuditEvent.Category.ADMIN,
                "description": "Deterministic restore drill fixture",
            },
        )
        self.stdout.write(self.style.SUCCESS("Restore drill fixture is present."))
