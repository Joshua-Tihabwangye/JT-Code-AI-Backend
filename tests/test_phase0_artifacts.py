"""Phase 0 artifact validation.

Phase 0 exit criteria: "all architecture decisions approved; definitive
repository inventory produced."

The checks here execute against the real artefacts rather than string-matching a
command's hardcoded output, so they fail when the implementation drifts.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

PROJECT_ROOT = Path(settings.BASE_DIR)
ADR_DIR = PROJECT_ROOT / "docs" / "adr"
DOCS_DIR = PROJECT_ROOT / "docs"

MANDATORY_ADRS = {
    "ADR-001": "ADR-001-supabase-auth-and-django-authorization.md",
    "ADR-002": "ADR-002-imagekit-removes-cloudinary.md",
    "ADR-003": "ADR-003-agentic-rag-vector-store.md",
    "ADR-004": "ADR-004-ai-gateway.md",
    "ADR-005": "ADR-005-kafka-celery-n8n-boundaries.md",
}

REQUIRED_SECTIONS = ("Context", "Decision", "Consequences", "Verification")
ACCEPTED_STATUSES = {"accepted", "adopted", "implemented"}


def _matrix(capsys) -> dict:
    call_command("inventory_matrix", format="json")
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# ADRs
# ---------------------------------------------------------------------------
def test_all_mandatory_adrs_present():
    existing = {p.name for p in ADR_DIR.glob("ADR-*.md")}
    assert set(MANDATORY_ADRS.values()).issubset(existing)


@pytest.mark.parametrize("adr_id", sorted(MANDATORY_ADRS))
def test_adr_is_well_formed(adr_id: str):
    path = ADR_DIR / MANDATORY_ADRS[adr_id]
    text = path.read_text(encoding="utf-8")

    for section in REQUIRED_SECTIONS:
        assert f"## {section}" in text, f"{path.name} is missing '## {section}'"

    status = re.search(r"\*\*Status:\*\*\s*([^\n]+)", text)
    assert status, f"{path.name} has no '**Status:**' header"
    assert status.group(1).strip().lower() in ACCEPTED_STATUSES, (
        f"{path.name} status is {status.group(1).strip()!r}, expected one of {sorted(ACCEPTED_STATUSES)}"
    )

    assert re.search(r"\*\*Date:\*\*\s*\d{4}-\d{2}-\d{2}", text), f"{path.name} has no ISO '**Date:**'"

    title = text.splitlines()[0]
    assert title.startswith(f"# {adr_id}:"), f"{path.name} title must start with '# {adr_id}:'"

    # A non-empty Decision section: an ADR that decides nothing is not approved.
    decision = text.split("## Decision", 1)[1].split("## Consequences", 1)[0]
    assert len(decision.strip()) > 200, f"{path.name} Decision section is too thin to be a real decision"


def test_adr_index_lists_every_adr_file():
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    for path in sorted(ADR_DIR.glob("ADR-*.md")):
        assert path.name in index, f"{path.name} is missing from the ADR index"
        assert path.stem.split("-", 2)[0] in index


def test_every_adr_is_registered_in_the_matrix(capsys):
    rows = {row["id"]: row for row in _matrix(capsys)["architecture_decisions"]}
    assert set(MANDATORY_ADRS).issubset(rows)
    for adr_id, row in rows.items():
        assert row["verified"], f"{adr_id} reported as missing a required section"
        assert row["status"].lower() in ACCEPTED_STATUSES, f"{adr_id} status is {row['status']!r}"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]), f"{adr_id} date is {row['date']!r}"


def test_matrix_rejects_a_non_accepted_adr(monkeypatch, tmp_path, capsys):
    """The matrix must fail closed when an ADR is not approved."""
    from apps.core.management.commands import inventory_matrix as module

    bad = tmp_path / "docs" / "adr"
    bad.mkdir(parents=True)
    (bad / "ADR-999-bad.md").write_text(
        "# ADR-999: bad\n\n**Status:** Draft\n\n**Date:** 2026-01-01\n\n"
        "## Context\n" + "x" * 300 + "\n\n## Decision\n" + "y" * 300 + "\n\n## Consequences\n" + "z" * 300 + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    with pytest.raises(CommandError, match="ADR-999"):
        call_command("inventory_matrix", format="json", strict=True)


# ---------------------------------------------------------------------------
# Inventory documents
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ("INVENTORY.md", "SECURITY.md", "SLOs.md", "BACKUP_RESTORE_RUNBOOK.md", "ARCHITECTURE.md"))
def test_inventory_document_exists(name: str):
    assert (DOCS_DIR / name).is_file(), f"docs/{name} is missing"


def test_security_doc_covers_every_threat_and_tier():
    text = (DOCS_DIR / "SECURITY.md").read_text(encoding="utf-8")
    for tier in ("C0", "C1", "C2", "C3"):
        assert tier in text, f"data classification tier {tier} is missing"
    threats = re.findall(r"^\| (T\d\d) ", text, flags=re.M)
    assert len(threats) >= 9, f"expected at least 9 threats, found {len(threats)}"
    assert threats == sorted(threats), "threats must be listed in order"
    for threat in threats:
        row = next(line for line in text.splitlines() if line.startswith(f"| {threat} "))
        assert len(row.split("|")) >= 5, f"{threat} row has no mitigation column"
        assert "TBD" not in row and "TODO" not in row, f"{threat} is not fully specified"


def test_slo_doc_has_identifier_objective_and_error_budget():
    text = (DOCS_DIR / "SLOs.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| ([A-Z-]+) \|", text, flags=re.M)
    assert {"AVI-API", "AVI-READY", "LAT-P95", "LAT-P99", "REC-RTO", "REC-RPO"}.issubset(rows)
    assert "RTO" in text and "RPO" in text
    assert "TBD" not in text


# ---------------------------------------------------------------------------
# Matrix correctness (executable, not string matching)
# ---------------------------------------------------------------------------
def test_matrix_is_scanned_from_source_not_hardcoded(capsys):
    report = _matrix(capsys)
    assert report["project_root"] == str(PROJECT_ROOT)
    assert report["source_files_scanned"] > 50, "the scan clearly did not run over the tree"
    assert report["generated_from"].startswith("config.settings")


def test_matrix_models_match_the_django_registry(capsys):
    from django.apps import apps as django_apps

    report = _matrix(capsys)
    matrix_models = {(row["app"], row["model"]) for row in report["models"]}
    registry = {
        (model._meta.app_label, model.__name__)
        for model in django_apps.get_models()
        if model._meta.app_label in report["migrations_by_app"]
    }
    assert matrix_models == registry, "the model matrix is not the Django app registry"


def test_matrix_endpoints_are_resolvable(capsys):
    from django.urls import resolve

    report = _matrix(capsys)
    assert len(report["endpoints"]) > 50
    for entry in report["endpoints"]:
        assert entry["name"], f"{entry['path']} has no URL name"
        assert entry["path"].startswith("/"), entry["path"]
        assert "(?P" not in entry["path"], f"unresolved regex left in {entry['path']}"
        # Every enumerated path must actually resolve.
        assert resolve(entry["path"] + "/").url_name or resolve(entry["path"]).url_name


def test_matrix_migration_counts_are_per_app(capsys):
    report = _matrix(capsys)
    for app_label, names in report["migrations_by_app"].items():
        assert names, f"{app_label} reported zero migrations"
        assert len(names) == len(set(names)), f"{app_label} has duplicate migration names"
    counts = {app: len(names) for app, names in report["migrations_by_app"].items()}
    # A project-wide total would be identical for every app; per-app counts differ.
    assert len(set(counts.values())) > 1, f"migration counts are not per-app: {counts}"
    assert counts["identity"] >= 5
    assert counts["governance"] >= 4


def test_matrix_env_vars_cover_every_settings_accessor(capsys):
    report = _matrix(capsys)
    env_vars = set(report["env_vars"])
    accessor = re.compile(
        r"(?:os\.getenv|env_bool|env_float|env_int|env_list|env)\(\s*['\"]([A-Z0-9_]+)['\"]"
    )
    from_source: set[str] = set()
    for path in (PROJECT_ROOT / "config" / "settings").glob("*.py"):
        from_source.update(accessor.findall(path.read_text(encoding="utf-8")))
    assert env_vars == from_source


def test_matrix_packages_reconcile_both_manifests(capsys):
    report = _matrix(capsys)
    packages = report["packages"]
    for required in ("django", "djangorestframework", "psycopg", "pyjwt", "pgvector", "celery", "weasyprint"):
        entry = packages[required]
        assert entry["pyproject"] == "yes", f"{required} is missing from pyproject.toml (Docker image would lack it)"
        assert entry["requirements"] == "yes", f"{required} is missing from requirements.txt"


def test_matrix_is_drift_free(capsys):
    """The whole point of a *verified* matrix: it must report zero problems."""
    report = _matrix(capsys)
    assert report["problems"] == [], f"inventory drift detected: {report['problems']}"


def test_strict_mode_fails_on_drift(monkeypatch, capsys):
    from apps.core.management.commands import inventory_matrix as module

    monkeypatch.setattr(
        module,
        "INTEGRATIONS",
        module.INTEGRATIONS
        + (
            module.IntegrationSpec(
                key="clerk",
                label="Clerk (retired)",
                distributions=("pyclerk",),
                modules=("jwt",),
                adr="ADR-001",
                retired=True,
            ),
        ),
    )
    with pytest.raises(CommandError, match="clerk"):
        call_command("inventory_matrix", format="json", strict=True)


# ---------------------------------------------------------------------------
# ADR removal guarantees
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("retired", ("clerk", "cloudinary", "pinecone"))
def test_retired_integrations_have_no_runtime_reference(retired: str):
    """ADR-001/002/003 removal guarantees, checked against real source."""
    needles = {
        "clerk": ("clerk", "pyclerk", "clerk_backend_api", "CLERK_"),
        "cloudinary": ("cloudinary", "CLOUDINARY_"),
        "pinecone": ("pinecone", "PINECONE_"),
    }[retired]
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "apps").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle.lower() in text.lower():
                offenders.append(f"{path.relative_to(PROJECT_ROOT)} mentions {needle}")
    assert not offenders, f"{retired} is referenced in runtime code: {offenders}"


def test_retired_integrations_absent_from_env_example():
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ("CLERK_SECRET_KEY", "CLERK_API_KEY", "CLOUDINARY_URL", "PINECONE_API_KEY"):
        assert var not in text, f"{var} must be removed from .env.example"


def test_no_unguarded_deprecated_sentry_api():
    """``sentry_sdk.configure_scope`` is removed in the next major version."""
    offenders = []
    for path in sorted((PROJECT_ROOT / "apps").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "configure_scope":
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
    assert not offenders, f"deprecated sentry_sdk.configure_scope used at {offenders}"
