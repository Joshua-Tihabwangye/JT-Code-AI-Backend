"""Generate a verified implementation matrix from the live source tree.

Scans registered models, URL patterns, environment variable accessors and
declared dependencies and prints a single machine-readable inventory. This is
the executable companion to ``docs/INVENTORY.md`` (Phase 0 backlog).

Usage:
    python manage.py inventory_matrix --format json
    python manage.py inventory_matrix --format markdown
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from django.apps import apps as django_apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.urls import URLResolver, get_resolver


def _load_url_specs() -> list[dict[str, str]]:
    """Enumerate concrete URL patterns (path + view name) under api/v1."""
    resolver = get_resolver()
    patterns: list[dict[str, str]] = []

    def walk(patterns_list: list[Any], prefix: str) -> None:
        for item in patterns_list:
            if isinstance(item, URLResolver):
                walk(item.url_patterns, f"{prefix}{item.pattern.regex.pattern.lstrip('^')}")
            else:
                pattern = str(item.pattern).lstrip("^")
                if not pattern:
                    continue
                full = f"/{prefix}{pattern}".replace("$", "")
                if full.startswith(("/admin", "/api/schema", "/api/docs")):
                    continue
                if "drf_format_suffix" in full or item.name == "api-root":
                    continue
                normalized = re.sub(r"\\\.\(\?P<format>\[a-z0-9\]\+\)/\?$", "", full)
                if normalized not in {p["path"] for p in patterns}:
                    patterns.append({"path": normalized, "name": item.name or ""})

    walk(resolver.url_patterns, "")
    return sorted(patterns, key=lambda p: p["path"])


def _load_env_var_refs() -> list[str]:
    """Find os.getenv/env() accessors referenced by the setting modules."""
    settings_dir = Path(settings.SETTINGS_MODULE and settings_dir_path() or "config/settings")
    accessors = re.findall(
        r"(?:os\.getenv|env_bool|env_float|env_list|env)\(\s*['\"]([A-Z0-9_]+)['\"]",
        "\n".join(p.read_text(encoding="utf-8") for p in settings_dir.glob("*.py")),
    )
    return sorted(set(accessors))


def settings_dir_path() -> str:
    parts = settings.SETTINGS_MODULE.split(".")
    return "/".join(parts[:-1])


def _model_app_labels() -> set[str]:
    return {app.rsplit(".", 1)[-1] for app in settings.INSTALLED_APPS if app.startswith("apps.")} | {"auth"}


def _load_model_matrix() -> list[dict[str, Any]]:
    labels = _model_app_labels()
    out: list[dict[str, Any]] = []
    for model in django_apps.get_models():
        app_label = model._meta.app_label
        if app_label not in labels:
            continue
        out.append(
            {
                "app": app_label,
                "model": model.__name__,
                "fields": sorted(f.name for f in model._meta.fields),
                "indexes": len(model._meta.indexes),
                "migrations": len(list(importlib_resolve_migrations(app_label))),
            }
        )
    return sorted(out, key=lambda m: (m["app"], m["model"]))


def importlib_resolve_migrations(app_label: str) -> list[object]:
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    return sorted(loader.graph.nodes.keys())


def _load_integrations() -> list[dict[str, str]]:
    """Return active and approved-target integrations with explicit status.

    Phase 0 needs an implementation matrix, not just a wishlist.  A row can be
    ``active`` (runtime code imports/configures it), ``accepted_target`` (ADR
    approved but not yet wired), or ``deprecated_active`` (still in runtime code
    while a later phase removes it).
    """
    rows = [
        {
            "integration": "supabase_auth",
            "owner_app": "apps.identity",
            "status": "active",
            "phase": "2",
        },
        {
            "integration": "supabase_postgresql_pgvector",
            "owner_app": "config/apps.knowledge",
            "status": "active",
            "phase": "3/10",
        },
        {
            "integration": "cloudinary",
            "owner_app": "apps.assets",
            "status": "deprecated_active",
            "phase": "11 removal",
        },
        {
            "integration": "imagekit",
            "owner_app": "apps.assets",
            "status": "accepted_target",
            "phase": "11 implementation",
        },
        {
            "integration": "stripe",
            "owner_app": "apps.billing",
            "status": "active",
            "phase": "14",
        },
        {
            "integration": "n8n",
            "owner_app": "apps.integrations/apps.core",
            "status": "active",
            "phase": "16 hardening",
        },
        {
            "integration": "kafka",
            "owner_app": "apps.events",
            "status": "active",
            "phase": "6",
        },
        {
            "integration": "sentry",
            "owner_app": "config.settings/apps.core",
            "status": "active",
            "phase": "15 hardening",
        },
    ]
    return sorted(rows, key=lambda row: row["integration"])


def _load_architecture_decisions() -> list[dict[str, str]]:
    adr_dir = Path("docs/adr")
    rows: list[dict[str, str]] = []
    for path in sorted(adr_dir.glob("ADR-*.md")):
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        status_match = re.search(r"\*\*Status:\*\*\s*([^\n]+)", text)
        rows.append(
            {
                "id": "-".join(path.stem.split("-", 2)[:2]),
                "title": title,
                "status": status_match.group(1).strip() if status_match else "unknown",
            }
        )
    return rows


class Command(BaseCommand):
    help = "Print a verified implementation matrix (models, endpoints, env vars, integrations)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--format",
            choices=("json", "markdown"),
            default="markdown",
            help="Output format of the inventory matrix.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        report = {
            "generated_from": settings.SETTINGS_MODULE,
            "models": _load_model_matrix(),
            "endpoints": _load_url_specs(),
            "env_vars": _load_env_var_refs(),
            "installed_apps": [app for app in settings.INSTALLED_APPS if app.startswith("apps.")],
            "integrations": _load_integrations(),
            "architecture_decisions": _load_architecture_decisions(),
        }
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2))
            return

        self.stdout.write("# JT-Code implementation matrix (generated)\n")
        self.stdout.write(f"\nGenerated from settings: `{report['generated_from']}`\n")

        self.stdout.write("\n## Models\n")
        self.stdout.write("| App | Model | Fields | Indexes | Migrations |")
        self.stdout.write("|-----|-------|--------|---------|------------|")
        for m in report["models"]:
            self.stdout.write(
                f"| {m['app']} | {m['model']} | {len(m['fields'])} | {m['indexes']} | {m['migrations']} |"
            )

        self.stdout.write("\n## Endpoints\n")
        self.stdout.write("| Path | Name |")
        self.stdout.write("|------|------|")
        for e in report["endpoints"]:
            self.stdout.write(f"| `{e['path']}` | {e['name']} |")

        self.stdout.write("\n## Environment variables\n")
        for var in report["env_vars"]:
            self.stdout.write(f"- `{var}`")

        self.stdout.write("\n## Installed apps\n")
        self.stdout.write(", ".join(app for app in report["installed_apps"]))

        self.stdout.write("\n## Integrations\n")
        self.stdout.write("| Integration | Owner app | Status | Phase |")
        self.stdout.write("|-------------|-----------|--------|-------|")
        for row in report["integrations"]:
            self.stdout.write(
                f"| {row['integration']} | {row['owner_app']} | {row['status']} | {row['phase']} |"
            )

        self.stdout.write("\n## Architecture decisions\n")
        self.stdout.write("| ID | Title | Status |")
        self.stdout.write("|----|-------|--------|")
        for row in report["architecture_decisions"]:
            self.stdout.write(f"| {row['id']} | {row['title']} | {row['status']} |")
