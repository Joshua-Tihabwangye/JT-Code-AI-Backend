from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction

from apps.billing.models import CreditLedger, CreditWallet
from apps.identity.models import Organization


class CreditService:
    """Service for managing credit wallets and ledger"""

    @staticmethod
    def get_or_create_wallet(organization: Organization) -> CreditWallet:
        wallet, created = CreditWallet.objects.get_or_create(
            organization=organization,
            defaults={
                "balance": Decimal("0"),
                "reserved_balance": Decimal("0"),
                "currency": "USD",
                "credit_value_usd": Decimal(str(settings.BILLING_CREDIT_VALUE_USD)),
            },
        )
        return wallet

    @staticmethod
    def _locked_wallet(organization: Organization) -> CreditWallet:
        CreditService.get_or_create_wallet(organization)
        return CreditWallet.objects.select_for_update().get(organization=organization)

    @staticmethod
    @transaction.atomic
    def add_credits(
        wallet: CreditWallet,
        amount: Decimal,
        reason: str,
        request_id: Any = None,
        metadata: dict[str, Any] | None = None,
        *,
        ledger_reason: str = CreditLedger.Reason.MANUAL_TOPUP,
        idempotency_key: str | None = None,
    ) -> CreditLedger:
        """Credit a wallet once per ``idempotency_key`` (defaults to the request id)."""
        locked = CreditWallet.objects.select_for_update().get(id=wallet.id)
        key = idempotency_key or f"topup_{request_id or uuid.uuid4()}"
        if existing := CreditLedger.objects.filter(wallet=locked, idempotency_key=key).first():
            return existing
        amount = Decimal(str(amount))
        locked.balance += amount
        locked.save(update_fields=["balance", "updated_at"])
        wallet.balance = locked.balance
        return CreditLedger.objects.create(
            wallet=locked,
            direction=CreditLedger.Direction.CREDIT,
            credits=amount,
            reason=ledger_reason,
            description=reason,
            request_id=request_id if _is_uuid(request_id) else None,
            idempotency_key=key,
            balance_after=locked.balance,
            metadata=metadata or {},
        )


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except TypeError, ValueError:
        return False
    return True
