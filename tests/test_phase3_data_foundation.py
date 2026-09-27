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


def test_identity_initial_migration_does_not_repeat_profile_fields():
    initial = (PROJECT_ROOT / "apps/identity/migrations/0001_initial.py").read_text(encoding="utf-8")
    follow_up = (PROJECT_ROOT / "apps/identity/migrations/0002_user_profile_fields.py").read_text(
        encoding="utf-8"
    )

    for field_name in ("bio", "contact", "country", "job_title", "timezone"):
        assert f"name='{field_name}'" in follow_up
        assert f"('{field_name}'," not in initial


def test_analytics_migration_uses_postgresql_view_syntax_and_reader_grants():
    views_path = PROJECT_ROOT / "apps/governance/migrations/0004_read_only_analytics_views.py"
    grants_path = PROJECT_ROOT / "apps/governance/migrations/0005_analytics_readonly_role.py"
    views_migration = views_path.read_text(encoding="utf-8")
    grants_migration = grants_path.read_text(encoding="utf-8")

    assert "CREATE OR REPLACE VIEW" in views_migration
    assert 'connection.vendor == "postgresql"' in views_migration
    assert "jt_code_analytics_reader" in grants_migration
    assert "GRANT SELECT" in grants_migration
