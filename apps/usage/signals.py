"""Allow the ledger-immutability triggers to permit cascades on tenant deletion.

The database rejects UPDATE/DELETE on usage records and credit-ledger rows
unless the transaction sets ``jt_code.ledger_purge``; deleting an organization
(an explicit, audited operation) is the only path that sets it.
"""

from __future__ import annotations

from typing import Any

from django.db import connection
from django.db.models.signals import pre_delete
from django.dispatch import receiver


@receiver(pre_delete, sender="identity.Organization")
def allow_ledger_purge(sender: Any, instance: Any, **kwargs: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('jt_code.ledger_purge', 'on', true)")
