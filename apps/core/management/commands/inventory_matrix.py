"""Generate a verified implementation matrix from the live source tree.

Phase 0 backlog: "Inventory every existing package, environment variable,
endpoint, model and integration" and "Generate a verified implementation matrix
from source code".

Everything this command emits is *derived from source* - models from the Django
app registry, endpoints from the URL resolver, environment variables from the
settings modules, packages from ``pyproject.toml``/``requirements.txt`` and
integrations from real import/config references in the runtime code. Nothing is
hardcoded, so the matrix cannot silently drift from the implementation.

Integration status is evidence-based rather than asserted:

``active``
    At least one runtime (non-migration) reference to the integration exists.
``declared_only``
    The distribution is declared as a dependency but nothing imports it.
``retired``
    The integration was removed by an ADR; a violation raises
    :class:`CommandError` in ``--strict`` mode.

Usage::

    python manage.py inventory_matrix --format markdown
    python manage.py inventory_matrix --format json
    python manage.py inventory_matrix --strict   # non-zero exit on drift
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.apps import apps as django_apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import URLResolver, get_resolver

#: Repository root, independent of the current working directory.
PROJECT_ROOT = Path(settings.BASE_DIR)

#: Directories that are runtime code for inventory purposes.
RUNTIME_DIRS = ("apps", "config")

#: Path prefixes excluded from reference scanning.
_SCAN_EXCLUDE_PARTS = {"migrations", "__pycache__", "tests", "node_modules", ".venv"}


@dataclass(frozen=True)
class IntegrationSpec:
    """Declarative detection rules for one external integration."""

    key: str
    label: str
    #: Distribution names in ``pyproject.toml`` / ``requirements.txt``.
    distributions: tuple[str, ...]
    #: Top-level Python modules that must be imported to use the integration.
    modules: tuple[str, ...]
    #: Django settings names whose presence configures the integration.
    settings_keys: tuple[str, ...] = ()
    #: ADR that governs the integration, if any.
    adr: str | None = None
    #: Retired integrations must have zero references anywhere in runtime code.
    retired: bool = False
    #: Purpose, surfaced in the generated report.
    purpose: str = ""


#: The single source of truth for integration detection.
#:
#: ``modules`` are what proves *runtime usage*; ``distributions`` is what
#: proves the package is *declared*. A drift between the two is reported.
# These are detection rules, not an inventory table: the report rows and every
# status/evidence field are derived by ``_classify_integrations`` from the
# runtime AST scan and dependency manifests. A small rule set is necessary
# for config-only services (for example ImageKit and n8n use ``httpx`` rather
# than a vendor SDK) and to make ADR retirement guarantees executable.
INTEGRATION_DETECTORS: tuple[IntegrationSpec, ...] = (
    IntegrationSpec(
        key="supabase_auth",
        label="Supabase Auth",
        distributions=("PyJWT",),
        modules=("jwt",),
        settings_keys=("SUPABASE_URL", "SUPABASE_JWT_SECRET", "SUPABASE_JWT_AUDIENCE", "SUPABASE_JWT_ISSUER"),
        adr="ADR-001",
        purpose="Session JWT/JWKS verification and user webhook (apps.identity)",
    ),
    IntegrationSpec(
        key="supabase_postgresql",
        label="Supabase PostgreSQL",
        distributions=("psycopg", "dj-database-url"),
        modules=("dj_database_url",),
        settings_keys=("DATABASE_URL",),
        purpose="Primary relational database (config.settings)",
    ),
    IntegrationSpec(
        key="supabase_postgresql_pgvector",
        label="Supabase PostgreSQL + pgvector",
        distributions=("pgvector",),
        modules=("pgvector",),
        settings_keys=("PGVECTOR_ENABLED", "VECTOR_EMBEDDING_DIMENSIONS"),
        adr="ADR-003",
        purpose="Active vector store; ADR-003 supersedes Pinecone (apps.knowledge)",
    ),
    IntegrationSpec(
        key="imagekit",
        label="ImageKit",
        distributions=(),
        modules=(),
        settings_keys=("IMAGEKIT_PUBLIC_KEY", "IMAGEKIT_PRIVATE_KEY", "IMAGEKIT_ENDPOINT_URL"),
        adr="ADR-002",
        purpose="Asset bytes and CDN via the ImageKit REST API (apps.assets)",
    ),
    IntegrationSpec(
        key="redis",
        label="Redis",
        distributions=("redis",),
        modules=(),
        settings_keys=("REDIS_URL",),
        purpose="Django cache backend (config.settings)",
    ),
    IntegrationSpec(
        key="celery",
        label="Celery / Celery Beat",
        distributions=("celery",),
        modules=("celery",),
        settings_keys=("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"),
        purpose="Background jobs and beat schedule (config/celery.py)",
    ),
    IntegrationSpec(
        key="kafka",
        label="Kafka",
        distributions=("confluent-kafka",),
        modules=("confluent_kafka",),
        settings_keys=("KAFKA_BOOTSTRAP_SERVERS", "KAFKA_TOPIC_PREFIX"),
        purpose="Transactional-outbox published events (apps.events)",
    ),
    IntegrationSpec(
        key="n8n",
        label="n8n",
        distributions=(),
        modules=(),
        settings_keys=("N8N_BASE_URL", "N8N_API_KEY", "N8N_WEBHOOK_SECRET"),
        adr="ADR-005",
        purpose="Workflow orchestration and signed callbacks (apps.integrations, apps.core)",
    ),
    IntegrationSpec(
        key="stripe",
        label="Stripe",
        distributions=("stripe",),
        modules=("stripe",),
        settings_keys=("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"),
        purpose="Checkout, subscriptions and signed webhooks (apps.billing)",
    ),
    IntegrationSpec(
        key="sentry",
        label="Sentry",
        distributions=("sentry-sdk",),
        modules=("sentry_sdk",),
        settings_keys=("SENTRY_DSN", "SENTRY_ENVIRONMENT"),
        purpose="Error monitoring, tracing and the n8n relay (config.settings, apps.core)",
    ),
    IntegrationSpec(
        key="openai",
        label="OpenAI",
        distributions=("openai",),
        modules=("openai",),
        settings_keys=("OPENAI_API_KEY",),
        purpose="Chat/completion and embeddings adapter (apps.ai_gateway, apps.knowledge)",
    ),
    IntegrationSpec(
        key="gemini",
        label="Google Gemini",
        distributions=("google-generativeai",),
        modules=("google.generativeai", "google"),
        settings_keys=("GEMINI_API_KEY", "GEMINI_EMBEDDING_MODEL"),
        purpose="Gemini chat and embeddings adapter (apps.ai_gateway, apps.knowledge)",
    ),
    IntegrationSpec(
        key="langgraph",
        label="LangGraph agent runtime",
        distributions=("langgraph", "langchain-core"),
        modules=("langgraph", "langchain_core"),
        adr="ADR-004",
        purpose="Tool-calling agent runtime (apps.agents)",
    ),
    IntegrationSpec(
        key="weasyprint",
        label="WeasyPrint",
        distributions=("weasyprint", "pydyf"),
        modules=("weasyprint",),
        purpose="HTML to PDF rendering (apps.documents)",
    ),
    IntegrationSpec(
        key="document_tooling",
        label="Document parsing (python-docx / pypdf / markdown)",
        distributions=("python-docx", "pypdf", "markdown"),
        modules=("docx", "pypdf", "markdown"),
        purpose="Source extraction and rendering (apps.documents, apps.knowledge)",
    ),
    # Retired by ADR-002. Any reference is a Phase 0 exit-criteria violation.
    IntegrationSpec(
        key="cloudinary",
        label="Cloudinary (retired by ADR-002)",
        distributions=("cloudinary",),
        modules=("cloudinary",),
        settings_keys=("CLOUDINARY_URL", "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY"),
        adr="ADR-002",
        retired=True,
        purpose="Removed; asset bytes are owned by ImageKit",
    ),
    # Retired by ADR-003. Any reference is a Phase 0 exit-criteria violation.
    IntegrationSpec(
        key="pinecone",
        label="Pinecone (retired by ADR-003)",
        distributions=("pinecone",),
        modules=("pinecone",),
        settings_keys=("PINECONE_API_KEY", "PINECONE_INDEX_NAME"),
        adr="ADR-003",
        retired=True,
        purpose="Removed; the vector store is Supabase pgvector",
    ),
    # Retired by ADR-001. Any reference is a Phase 0 exit-criteria violation.
    IntegrationSpec(
        key="clerk",
        label="Clerk (retired by ADR-001)",
        distributions=("clerk-backend-api", "pyclerk"),
        modules=("clerk", "pyclerk", "clerk_backend_api"),
        settings_keys=("CLERK_SECRET_KEY", "CLERK_API_KEY", "CLERK_JWT_SECRET", "CLERK_WEBHOOK_SECRET"),
        adr="ADR-001",
        retired=True,
        purpose="Removed; Supabase Auth is the sole identity provider",
    ),
)

#: Architecture-freeze ADRs required by Phase 0. Their contents are parsed
#: from disk; this set only defines the approved decision identifiers.
REQUIRED_ADR_IDS = frozenset({"ADR-001", "ADR-002", "ADR-003", "ADR-004", "ADR-005"})

#: Top-level modules that are part of the standard library or the Django
#: project itself and therefore never reported as third-party.
_STDLIB = frozenset(sys.stdlib_module_names) if (sys := __import__("sys")) else frozenset()

_LOCAL_MODULES = frozenset({"config", "apps", "manage", "tests"})

#: Framework and support libraries that are expected in any Django service.
#: They are inventoried as *packages*; they are not *integrations*, so they
#: must not be reported as unclassified third-party imports.
_FRAMEWORK_MODULES = frozenset(
    {
        "django",
        "rest_framework",
        "drf_spectacular",
        "corsheaders",
        "dotenv",
        "httpx",
        "PIL",
        "dj_database_url",
        "psycopg",
        "pytest",
    }
)


@dataclass
class ScanResult:
    """Everything the source scan observed, kept for reuse and reporting."""

    imports: dict[str, set[str]] = field(default_factory=dict)
    env_vars: set[str] = field(default_factory=set)
    files_scanned: int = 0
    raw_text: dict[str, str] = field(default_factory=dict)


def _iter_source_files(include_tests: bool = False) -> list[Path]:
    """Return every Python file that counts as repository source."""
    excluded = set(_SCAN_EXCLUDE_PARTS)
    if not include_tests:
        excluded = excluded | {"tests"}
    files: list[Path] = []
    for directory in RUNTIME_DIRS:
        root = PROJECT_ROOT / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if excluded & set(path.parts):
                continue
            files.append(path)
    return sorted(files)


def _scan_source(include_tests: bool = False) -> ScanResult:
    """Parse runtime source and collect imports plus environment variables.

    Uses :mod:`ast` rather than regular expressions so that an identifier
    mentioned in a docstring or comment is never mistaken for a dependency.
    """
    result = ScanResult()
    for path in _iter_source_files(include_tests=include_tests):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - defensive
            raise CommandError(f"Cannot parse {path}: {exc}") from exc
        result.files_scanned += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    result.imports.setdefault(alias.name.split(".")[0], set()).add(str(path))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                result.imports.setdefault(node.module.split(".")[0], set()).add(str(path))

    settings_files = sorted((PROJECT_ROOT / "config" / "settings").glob("*.py"))
    env_pattern = re.compile(
        r"(?:os\.getenv|env_bool|env_float|env_int|env_list|env)\(\s*['\"]([A-Z0-9_]+)['\"]"
    )
    for path in settings_files:
        text = path.read_text(encoding="utf-8")
        result.env_vars.update(env_pattern.findall(text))
    return result


def _third_party_modules(scan: ScanResult) -> dict[str, set[str]]:
    """Top-level imported modules that are neither stdlib nor project-local."""
    out: dict[str, set[str]] = {}
    for module, files in scan.imports.items():
        if module in _STDLIB or module in _LOCAL_MODULES or module in _FRAMEWORK_MODULES:
            continue
        if module.startswith("_"):
            continue
        out[module] = files
    return out


def _load_dependencies() -> dict[str, dict[str, str]]:
    """Parse declared dependencies from ``pyproject.toml`` and ``requirements.txt``."""
    import tomllib

    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    requirements_path = PROJECT_ROOT / "requirements.txt"
    if not pyproject_path.is_file():  # pragma: no cover - defensive
        raise CommandError("pyproject.toml is missing; cannot build the package inventory.")

    def normalise(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name.split("[")[0]).strip().lower()

    with pyproject_path.open("rb") as handle:
        pyproject = tomllib.load(handle)

    declared: dict[str, dict[str, str]] = {}

    def add_toml_dependency(requirement: str, *, dev: bool) -> None:
        match = re.match(r"^([A-Za-z0-9._-]+(?:\[[^\]]+\])?)\s*(.*)$", requirement.strip())
        if not match:
            return
        name = normalise(match.group(1))
        entry = declared.setdefault(
            name,
            {"version": "", "pyproject": "no", "requirements": "no", "dev": "no"},
        )
        entry["pyproject"] = "yes"
        entry["dev"] = "yes" if dev else entry["dev"]
        if entry["version"] in ("", "*"):
            entry["version"] = match.group(2).strip() or "*"

    for requirement in pyproject.get("project", {}).get("dependencies", []):
        add_toml_dependency(requirement, dev=False)
    for requirement in pyproject.get("project", {}).get("optional-dependencies", {}).get("dev", []):
        add_toml_dependency(requirement, dev=True)

    if requirements_path.is_file():
        for line in requirements_path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            match = re.match(r"^([A-Za-z0-9._-]+(?:\[[^\]]+\])?)", line)
            if not match:
                continue
            name = normalise(match.group(1))
            entry = declared.setdefault(
                name,
                {"version": "", "pyproject": "no", "requirements": "no", "dev": "no"},
            )
            entry["requirements"] = "yes"
            if entry["version"] in ("", "*"):
                entry["version"] = line
    return dict(sorted(declared.items()))


def _dependency_drift(dependencies: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    """Dependencies declared in only one of the two manifest files.

    ``pyproject.toml`` is authoritative because ``Dockerfile`` installs with
    ``pip install .``; anything present only in ``requirements.txt`` is absent
    from the production image.
    """
    drift: list[dict[str, str]] = []
    for name, entry in dependencies.items():
        if entry["pyproject"] == "yes" and entry["requirements"] == "no":
            if entry.get("dev") == "yes":
                continue  # dev-only tools legitimately live in pyproject alone
            drift.append({"package": name, "problem": "declared in pyproject.toml only"})
        elif entry["pyproject"] == "no" and entry["requirements"] == "yes":
            drift.append(
                {
                    "package": name,
                    "problem": "declared in requirements.txt only (missing from the Docker image)",
                }
            )
    return drift


def _classify_integrations(
    scan: ScanResult,
    dependencies: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Derive integration status from real source evidence."""
    declared = set(dependencies)

    def normalise(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).strip().lower()

    rows: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []

    for spec in INTEGRATION_DETECTORS:
        module_evidence: dict[str, list[str]] = {}
        for module in spec.modules:
            if module in scan.imports:
                module_evidence[module] = sorted(scan.imports[module])
        settings_evidence = sorted(key for key in spec.settings_keys if key in scan.env_vars)
        wanted = {normalise(d) for d in spec.distributions}
        distributions = sorted(d for d in declared if d in wanted)
        missing_distributions = sorted(d for d in spec.distributions if normalise(d) not in declared)

        referenced = bool(module_evidence) or bool(settings_evidence)
        if spec.retired:
            status = "retired"
            if referenced:
                problems.append(
                    {
                        "integration": spec.key,
                        "problem": (
                            f"retired by {spec.adr} but still referenced at "
                            + ", ".join(sorted({f for fs in module_evidence.values() for f in fs}))
                        ),
                    }
                )
        elif referenced:
            status = "active"
        else:
            status = "declared_only"

        if status == "active" and spec.distributions and not distributions:
            problems.append(
                {
                    "integration": spec.key,
                    "problem": "used at runtime but its distribution is not declared in pyproject.toml",
                }
            )
        if status == "declared_only":
            problems.append(
                {
                    "integration": spec.key,
                    "problem": (
                        "distribution is declared but nothing imports it; wire it or drop the dependency"
                    ),
                }
            )

        rows.append(
            {
                "integration": spec.key,
                "label": spec.label,
                "status": status,
                "adr": spec.adr,
                "purpose": spec.purpose,
                "imported_modules": sorted(module_evidence),
                "configured_by_settings": settings_evidence,
                "distributions": distributions or missing_distributions,
                "evidence": sorted({f for fs in module_evidence.values() for f in fs})[:6],
            }
        )

    known_modules = {module for spec in INTEGRATION_DETECTORS for module in spec.modules}
    for module, files in sorted(_third_party_modules(scan).items()):
        if module in known_modules:
            continue
        problems.append(
            {
                "integration": f"unclassified:{module}",
                "problem": "third-party import with no entry in INTEGRATIONS: "
                + ", ".join(sorted(files)[:3]),
            }
        )
    return rows, problems


def _load_url_specs() -> list[dict[str, str]]:
    """Enumerate concrete URL patterns (path + view name) under the API root."""
    resolver = get_resolver()
    patterns: list[dict[str, str]] = []
    seen: set[str] = set()

    def walk(patterns_list: Any, prefix: str) -> None:
        for item in patterns_list:
            if isinstance(item, URLResolver):
                walk(item.url_patterns, f"{prefix}{str(item.pattern).lstrip('^')}")
            else:
                pattern = str(item.pattern).lstrip("^")
                if not pattern or item.name in {"api-root", "schema", "schema-ui", "redoc"}:
                    continue
                full = f"/{prefix}{pattern}".replace("$", "")
                if full.startswith(("/admin", "/api/schema", "/api/docs", "/__debug__")):
                    continue
                if "drf_format_suffix" in full or "(?P<format>" in full:
                    continue
                # DRF routers expose regex patterns; report a stable, readable
                # route template instead of leaking Django's implementation syntax.
                full = re.sub(r"\(\?P<([^>]+)>[^)]+\)", r"{\1}", full)
                full = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", full)
                normalised = re.sub(r"/?$", "", full)
                if normalised in seen:
                    continue
                seen.add(normalised)
                patterns.append({"path": normalised, "name": item.name or ""})

    walk(resolver.url_patterns, "")
    return sorted(patterns, key=lambda entry: entry["path"])


def _per_app_migration_counts() -> dict[str, list[str]]:
    """Migration names grouped by app label, from the real migration graph."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    grouped: dict[str, list[str]] = {}
    for app_label, name in loader.graph.nodes:
        if app_label == "__first__" or name is None:
            continue
        grouped.setdefault(app_label, []).append(name)
    return {label: sorted(names) for label, names in sorted(grouped.items())}


def _model_app_labels() -> set[str]:
    labels = {app.rsplit(".", 1)[-1] for app in settings.INSTALLED_APPS if app.startswith("apps.")}
    return labels | {"auth"}


def _load_model_matrix(migrations: dict[str, list[str]]) -> list[dict[str, Any]]:
    labels = _model_app_labels()
    rows: list[dict[str, Any]] = []
    for model in django_apps.get_models():
        app_label = model._meta.app_label
        if app_label not in labels:
            continue
        field_names = {f.name for f in model._meta.fields}
        rows.append(
            {
                "app": app_label,
                "model": model.__name__,
                "table": model._meta.db_table,
                "fields": sorted(field_names),
                "field_count": len(field_names),
                "has_organization": "organization" in field_names,
                "indexes": len(model._meta.indexes) + len(model._meta.constraints),
                "migrations": migrations.get(app_label, []),
                "migration_count": len(migrations.get(app_label, [])),
            }
        )
    return sorted(rows, key=lambda row: (row["app"], row["model"]))


def _load_architecture_decisions() -> list[dict[str, str]]:
    """Read ADR metadata from ``docs/adr`` relative to the repository root."""
    adr_dir = PROJECT_ROOT / "docs" / "adr"
    rows: list[dict[str, str]] = []
    for path in sorted(adr_dir.glob("ADR-*.md")):
        text = path.read_text(encoding="utf-8")
        status = re.search(r"\*\*Status:\*\*\s*([^\n]+)", text)
        date = re.search(r"\*\*Date:\*\*\s*([^\n]+)", text)
        identifier = "-".join(path.stem.split("-", 2)[:2])
        title = text.splitlines()[0].lstrip("# ").strip() if text.splitlines() else ""
        rows.append(
            {
                "id": identifier,
                "file": path.name,
                "title": title,
                "status": status.group(1).strip() if status else "unknown",
                "date": date.group(1).strip() if date else "unknown",
                "verified": (
                    bool(re.fullmatch(r"ADR-\d{3}", identifier))
                    and title.startswith(f"{identifier}:")
                    and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", date.group(1).strip() if date else ""))
                    and all(
                        f"## {section}" in text
                        for section in ("Context", "Decision", "Consequences", "Verification")
                    )
                ),
            }
        )
    return rows


def _admission_problems(adr_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Phase 0 exit criteria: every ADR approved and well formed."""
    problems: list[dict[str, str]] = []
    accepted = {"accepted", "adopted", "implemented"}
    present = {row["id"] for row in adr_rows}
    for identifier in sorted(REQUIRED_ADR_IDS - present):
        problems.append({"integration": identifier, "problem": "required Phase 0 ADR is missing"})
    for row in adr_rows:
        if not row["verified"]:
            problems.append(
                {
                    "integration": row["id"],
                    "problem": (
                        "invalid ADR id/title/date or missing required "
                        "Context/Decision/Consequences/Verification section"
                    ),
                }
            )
        if row["status"].lower() not in accepted:
            problems.append(
                {"integration": row["id"], "problem": f"status is {row['status']!r}, expected Accepted"}
            )
    identifiers = [row["id"] for row in adr_rows]
    duplicates = sorted({identifier for identifier in identifiers if identifiers.count(identifier) > 1})
    for identifier in duplicates:
        problems.append({"integration": identifier, "problem": "duplicate ADR identifier"})

    index = PROJECT_ROOT / "docs" / "adr" / "README.md"
    if index.is_file():
        index_text = index.read_text(encoding="utf-8")
        for row in adr_rows:
            if row["id"] not in index_text:
                problems.append(
                    {"integration": row["id"], "problem": "missing from docs/adr/README.md index"}
                )
    return problems


class Command(BaseCommand):
    help = "Print a verified implementation matrix (models, endpoints, env vars, packages, integrations)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--format",
            choices=("json", "markdown"),
            default="markdown",
            help="Output format of the inventory matrix.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help=(
                "Exit non-zero when the matrix finds drift (retired integrations, "
                "unclassified imports, dependency drift)."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        scan = _scan_source()
        dependencies = _load_dependencies()
        migrations = _per_app_migration_counts()
        integration_rows, integration_problems = _classify_integrations(scan, dependencies)
        adr_rows = _load_architecture_decisions()

        env_vars = sorted(scan.env_vars)
        drift = _dependency_drift(dependencies)
        problems = list(integration_problems) + _admission_problems(adr_rows)
        problems.extend({"integration": f"package:{d['package']}", "problem": d["problem"]} for d in drift)

        report = {
            "generated_from": settings.SETTINGS_MODULE,
            "project_root": str(PROJECT_ROOT),
            "source_files_scanned": scan.files_scanned,
            "models": _load_model_matrix(migrations),
            "endpoints": _load_url_specs(),
            "env_vars": env_vars,
            "packages": dependencies,
            "integrations": integration_rows,
            "architecture_decisions": adr_rows,
            "migrations_by_app": migrations,
            "third_party_modules": sorted(_third_party_modules(scan)),
            "problems": problems,
        }

        if options["strict"] and problems:
            detail = "\n".join(f"  - {p['integration']}: {p['problem']}" for p in problems)
            raise CommandError(f"Inventory matrix found {len(problems)} problem(s):\n{detail}")

        if options["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        self._write_markdown(report)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _write_markdown(self, report: dict[str, Any]) -> None:
        w = self.stdout.write
        w("# JT-Code implementation matrix (generated)\n\n")
        w(
            f"Generated from settings `{report['generated_from']}`, "
            f"{report['source_files_scanned']} source files scanned.\n\n"
        )

        problems = report["problems"]
        w("## Verification\n\n")
        if problems:
            w(f"**{len(problems)} problem(s) detected:**\n\n")
            for problem in problems:
                w(f"- `{problem['integration']}`: {problem['problem']}\n")
        else:
            w(
                "No drift detected: no retired integration is referenced, every third-party "
                "import is classified,\n"
                "and the two dependency manifests agree.\n"
            )
        w("\n")

        w("## Models\n\n")
        w("| App | Model | Table | Fields | Indexes | Migrations | Tenant-scoped |\n")
        w("|-----|-------|-------|--------|---------|------------|---------------|\n")
        for row in report["models"]:
            w(
                f"| {row['app']} | {row['model']} | `{row['table']}` | {row['field_count']} | "
                f"{row['indexes']} | {row['migration_count']} | "
                f"{'yes' if row['has_organization'] else 'no'} |\n"
            )

        w("\n## Endpoints\n\n| Path | Name |\n|------|------|\n")
        for row in report["endpoints"]:
            w(f"| `{row['path']}` | `{row['name']}` |\n")

        w("\n## Environment variables\n\n")
        for var in report["env_vars"]:
            w(f"- `{var}`\n")

        w("\n## Packages\n\n")
        w("| Package | Constraint | pyproject.toml | requirements.txt |\n")
        w("|---------|-----------|----------------|------------------|\n")
        for name, entry in report["packages"].items():
            w(f"| {name} | `{entry['version']}` | {entry['pyproject']} | {entry['requirements']} |\n")

        w("\n## Integrations\n\n")
        w("| Integration | Status | ADR | Configured by | Evidence |\n")
        w("|-------------|--------|-----|---------------|----------|\n")
        for row in report["integrations"]:
            evidence = ", ".join(f"`{e}`" for e in row["evidence"]) or "-"
            configured = ", ".join(f"`{c}`" for c in row["configured_by_settings"]) or "-"
            w(f"| {row['label']} | **{row['status']}** | {row['adr'] or '-'} | {configured} | {evidence} |\n")

        w("\n## Architecture decisions\n\n")
        w("| ID | Title | Status | Date | Well formed |\n|----|-------|--------|------|-------------|\n")
        for row in report["architecture_decisions"]:
            w(
                f"| {row['id']} | {row['title']} | {row['status']} | {row['date']} | "
                f"{'yes' if row['verified'] else 'NO'} |\n"
            )
