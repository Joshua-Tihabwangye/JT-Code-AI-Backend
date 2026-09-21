"""Phase 0 artifacts: ADRs, inventory matrix command and security/SLO docs."""

from pathlib import Path

import pytest
from django.core.management import call_command

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = PROJECT_ROOT / "docs" / "adr"
MANDATORY_ADRS = {
    "ADR-001-supabase-auth-and-django-authorization.md",
    "ADR-002-imagekit-removes-cloudinary.md",
    "ADR-003-agentic-rag-vector-store.md",
    "ADR-004-ai-gateway.md",
    "ADR-005-kafka-celery-n8n-boundaries.md",
}


def _collect_adr_paths() -> list[Path]:
    return sorted(ADR_DIR.glob("ADR-*.md"))


def test_all_five_adrs_present():
    existing = {p.name for p in _collect_adr_paths()}
    assert MANDATORY_ADRS.issubset(existing)


@pytest.mark.parametrize("section", ("Context", "Decision", "Consequences"))
def test_each_adr_has_required_sections(section):
    for path in _collect_adr_paths():
        text = path.read_text(encoding="utf-8")
        assert f"## {section}" in text, f"{path.name} is missing section {section}"


def test_adr_readme_index_lists_all_adrs():
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    for name in MANDATORY_ADRS:
        adr_id = name.split("-", 1)[0]
        assert adr_id in index, f"{adr_id} missing from ADR index"


def test_inventory_document_exists():
    assert (PROJECT_ROOT / "docs" / "INVENTORY.md").is_file()


def test_security_and_slo_docs_exist():
    assert (PROJECT_ROOT / "docs" / "SECURITY.md").is_file()
    assert (PROJECT_ROOT / "docs" / "SLOs.md").is_file()


def test_inventory_matrix_command_runs_and_is_complete(capsys):
    call_command("inventory_matrix", format="json")

    import json

    stdout = capsys.readouterr().out
    report = json.loads(stdout)
    assert report["generated_from"].startswith("config.settings")
    assert len(report["models"]) > 0
    assert len(report["endpoints"]) > 0
    assert len(report["env_vars"]) > 0
    assert "apps.identity" in report["installed_apps"]
    assert any("User" in m["model"] for m in report["models"])
    integrations = {row["integration"]: row for row in report["integrations"]}
    assert integrations["supabase_postgresql_pgvector"]["status"] == "active"
    assert integrations["cloudinary"]["status"] == "deprecated_active"
    assert integrations["imagekit"]["status"] == "accepted_target"
    assert {row["id"] for row in report["architecture_decisions"]} >= {
        "ADR-001",
        "ADR-002",
        "ADR-003",
        "ADR-004",
        "ADR-005",
    }


def test_inventory_matrix_markdown_output(capsys):
    call_command("inventory_matrix", format="markdown")
    stdout = capsys.readouterr().out
    assert "# JT-Code implementation matrix" in stdout
    assert "## Models" in stdout
    assert "## Endpoints" in stdout
    assert "## Environment variables" in stdout
    assert "## Integrations" in stdout
    assert "## Architecture decisions" in stdout


def test_vector_store_adr_reconciles_pinecone_backlog_item():
    text = (ADR_DIR / "ADR-003-agentic-rag-vector-store.md").read_text(encoding="utf-8")
    assert "Backlog reconciliation" in text
    assert "Pinecone" in text
    assert "Supabase PostgreSQL + pgvector" in text
