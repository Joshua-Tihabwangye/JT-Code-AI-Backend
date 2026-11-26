"""The committed env examples hold placeholders only (G5); real credentials go in the untracked env files."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = [".env.example", ".env.docker.example", ".env.production.example", "infra/n8n/n8n.env.example"]
# Shapes of real credentials: Gmail app passwords, live Stripe keys, Google API keys, JWTs, Slack tokens.
REAL_SECRET = re.compile(
    r"^[a-z]{16}$|^([a-z]{4} ){3}[a-z]{4}$|^(sk|rk)_live_[0-9A-Za-z]{10,}|^whsec_[0-9A-Za-z]{20,}"
    r"|^AIza[0-9A-Za-z_-]{30,}|^eyJ[\w-]{20,}\.eyJ[\w-]{20,}\.|^xox[abp]-|^AKIA[0-9A-Z]{16}"
)


def _values(path: str) -> dict[str, str]:
    values = {}
    for line in (ROOT / path).read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.split(" #", 1)[0].strip().strip("'\"")
    return values


@pytest.mark.parametrize("path", EXAMPLES)
def test_examples_hold_no_real_credentials_or_personal_addresses(path):
    # A real Gmail app password and the operator's address were committed here once (P17-1).
    for key, value in _values(path).items():
        assert not REAL_SECRET.search(value), f"{path}: {key} looks like a real credential"
        assert "@gmail.com" not in value.lower(), f"{path}: {key} holds a personal address"


@pytest.mark.parametrize("path", [".env.example", ".env.docker.example", ".env.production.example"])
def test_example_smtp_passwords_are_empty(path):
    assert _values(path).get("SMTP_PASSWORD", "") == ""


def test_the_local_example_sends_auth_mail_to_mailpit():
    values = _values(".env.docker.example")
    assert (values["SMTP_HOST"], values["SMTP_PORT"]) == ("mailpit", "1025")
