from __future__ import annotations

from pathlib import Path

import pytest
from django.db import connection


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYTICS_VIEWS = (
    "analytics_job_summary",
    "analytics_usage_ledger",
    "analytics_billing_summary",
    "analytics_conversation_summary",
    "analytics_asset_summary",
)


@pytest.mark.django_db
def test_phase3_read_only_analytics_views_are_queryable():
    with connection.cursor() as cursor:
        for view_name in ANALYTICS_VIEWS:
            cursor.execute(f"SELECT COUNT(*) FROM {view_name}")
            assert cursor.fetchone()[0] >= 0


def test_backup_restore_runbook_covers_pitr_restore_and_analytics_views():
    runbook = (PROJECT_ROOT / "docs" / "BACKUP_RESTORE_RUNBOOK.md").read_text(encoding="utf-8")

    assert "Point-in-Time Recovery" in runbook
    assert "sslmode=require" in runbook
    assert "RTO <= 4 hours" in runbook
    assert "RPO <= 1 hour" in runbook
    for view_name in ANALYTICS_VIEWS:
        assert view_name in runbook
