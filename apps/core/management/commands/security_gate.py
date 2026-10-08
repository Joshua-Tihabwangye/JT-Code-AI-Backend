"""Run the security test gate: SAST, dependency audit, secret scan and DAST results.

    python manage.py security_gate                       # bandit + pip-audit + detect-secrets
    python manage.py security_gate --zap-report r.json   # also fail on FAIL-level ZAP alerts

Each scanner is a separate, pinned dev dependency invoked with a fixed argv
(no shell). The command exits non-zero when any gate fails, so CI and a
developer machine apply the same policy.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - fixed scanner argv, shell=False
import sys
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

_RISK_NAMES = {"0": "informational", "1": "low", "2": "medium", "3": "high"}


def _tool(name: str) -> str:
    candidate = Path(sys.executable).with_name(name)
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise CommandError(f"{name} is not installed; install the dev extras (pip install -e '.[dev]').")
    return found


def zap_failures(report: dict[str, Any], rules: dict[str, str]) -> list[str]:
    """FAIL-level ZAP alerts, plus any unlisted alert of high risk."""
    failures = []
    for site in report.get("site") or []:
        for alert in site.get("alerts") or []:
            plugin = str(alert.get("pluginid", ""))
            level = rules.get(plugin)
            risk = str(alert.get("riskcode", "0"))
            if level == "FAIL" or (level is None and risk == "3"):
                failures.append(f"{plugin} {alert.get('name', '')} ({_RISK_NAMES.get(risk, risk)})")
    return failures


def parse_rules(path: Path) -> dict[str, str]:
    rules = {}
    for line in path.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip().isdigit():
            rules[parts[0].strip()] = parts[1].strip().upper()
    return rules


class Command(BaseCommand):
    help = "Run SAST (bandit), dependency (pip-audit), secret (detect-secrets) and DAST (ZAP) gates."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--skip-scanners", action="store_true", help="Only evaluate a ZAP report.")
        parser.add_argument("--skip-dependency-audit", action="store_true", help="Offline runs.")
        parser.add_argument("--zap-report", type=Path, help="ZAP JSON report (zap-api-scan.py -J).")
        parser.add_argument("--zap-rules", type=Path, default=Path(settings.BASE_DIR) / ".zap" / "rules.tsv")

    def _run(self, name: str, argv: list[str]) -> bool:
        self.stdout.write(f"==> {name}")
        result = subprocess.run(  # nosec B603 - fixed argv built from installed scanner paths
            argv, cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=1200, check=False
        )
        if result.returncode:
            self.stdout.write((result.stdout + result.stderr)[-4000:])
            self.stdout.write(self.style.ERROR(f"FAIL {name}"))
            return False
        self.stdout.write(self.style.SUCCESS(f"PASS {name}"))
        return True

    def handle(self, *args: Any, **options: Any) -> None:
        passed = True
        if not options["skip_scanners"]:
            passed &= self._run(
                "SAST (bandit)",
                [_tool("bandit"), "-q", "-r", "apps", "config", "manage.py", "--exclude", "tests"],
            )
            files = subprocess.run(  # nosec B603 B607 - fixed git argv
                # Tracked and new (untracked, not ignored) files: scan code before it is committed.
                ["git", "ls-files", "-co", "--exclude-standard"],
                cwd=settings.BASE_DIR,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
            passed &= self._run(
                "Secret scan (detect-secrets)",
                [_tool("detect-secrets-hook"), "--baseline", ".secrets.baseline", *files],
            )
            if not options["skip_dependency_audit"]:
                passed &= self._run(
                    "Dependency audit (pip-audit)", [_tool("pip-audit"), "--strict", "-r", "requirements.txt"]
                )
        if report_path := options["zap_report"]:
            self.stdout.write("==> DAST (OWASP ZAP report)")
            failures = zap_failures(json.loads(report_path.read_text()), parse_rules(options["zap_rules"]))
            for failure in failures:
                self.stdout.write(self.style.ERROR(f"  {failure}"))
            if failures:
                self.stdout.write(self.style.ERROR("FAIL DAST"))
                passed = False
            else:
                self.stdout.write(self.style.SUCCESS("PASS DAST"))
        if not passed:
            raise CommandError("Security gate failed.")
        self.stdout.write(self.style.SUCCESS("Security gate passed."))
