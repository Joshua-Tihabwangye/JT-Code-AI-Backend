"""Supabase Auth (GoTrue) and gateway hardening (backlog Phase 3).

nginx is not installed on developer machines, so the gateway is checked statically here; the live checks
(429 on a burst, CORS from a foreign origin, /rest/v1 closed) are operator steps after `make docker-up`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "docker/supabase/gateway"
TEMPLATE = (GATEWAY / "default.conf.template").read_text()


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text().replace("!override", "").replace("!reset", ""))


def _server(port: int) -> str:
    """The text of the ``server { listen <port>; ... }`` block."""
    start = TEMPLATE.index(f"listen {port};")
    following = TEMPLATE.find("\nserver {", start)
    return TEMPLATE[start : following if following != -1 else len(TEMPLATE)]


def _location(block: str, prefix: str) -> str:
    start = block.index(f"location {prefix} {{")
    return block[start : block.index("\n    }", start)]


# GoTrue ----------------------------------------------------------------------------------------


def test_gotrue_enforces_the_signup_form_password_policy():
    env = _compose()["services"]["supabase-auth"]["environment"]

    assert env["GOTRUE_PASSWORD_MIN_LENGTH"] == "${SUPABASE_PASSWORD_MIN_LENGTH:-8}"
    groups = env["GOTRUE_PASSWORD_REQUIRED_CHARACTERS"].split(":")
    assert set("abcxyzABCXYZ") <= set(groups[0]) and set(groups[1]) == set("0123456789")


def test_frontend_password_rules_match_the_server_when_the_frontend_is_present():
    schema = ROOT.parent / "jt-code-frontend/src/features/auth/signup/schema.ts"
    if not schema.exists():
        pytest.skip("frontend repository not checked out next to the backend")
    text = schema.read_text()
    assert ".min(8, 'authErrors.passwordMinLength')" in text
    assert "/[A-Za-z]/" in text and "/[0-9]/" in text


def test_gotrue_session_and_account_security():
    env = _compose()["services"]["supabase-auth"]["environment"]

    assert env["GOTRUE_SECURITY_REFRESH_TOKEN_ROTATION_ENABLED"] == "true"
    assert env["GOTRUE_SECURITY_UPDATE_PASSWORD_REQUIRE_REAUTHENTICATION"] == "true"
    assert env["GOTRUE_MAILER_SECURE_EMAIL_CHANGE_ENABLED"] == "true"
    assert env["GOTRUE_EXTERNAL_ANONYMOUS_USERS_ENABLED"] == "false"
    assert env["GOTRUE_SECURITY_MANUAL_LINKING_ENABLED"] == "false"
    assert env["GOTRUE_SESSIONS_INACTIVITY_TIMEOUT"].endswith(":-168h}")
    assert env["GOTRUE_SESSIONS_TIMEBOX"].endswith(":-720h}")


def test_gotrue_rate_limits_are_per_client_ip():
    env = _compose()["services"]["supabase-auth"]["environment"]

    assert env["GOTRUE_RATE_LIMIT_HEADER"] == "X-JT-Client-IP"
    for limit in ("EMAIL_SENT", "VERIFY", "TOKEN_REFRESH", "OTP", "SSO"):
        assert f"GOTRUE_RATE_LIMIT_{limit}" in env
    # GoTrue is reachable only through the gateway, which sets the header; it publishes no port.
    assert "ports" not in _compose()["services"]["supabase-auth"]


# Gateway ---------------------------------------------------------------------------------------


def test_gateway_publishes_only_the_public_listener_and_django_uses_the_internal_one():
    compose = _compose()
    gateway = compose["services"]["supabase-gateway"]

    assert gateway["ports"] == ["127.0.0.1:54321:8000"]
    assert compose["x-app-env"]["SUPABASE_INTERNAL_URL"] == "http://supabase-gateway:8001"
    mounts = " ".join(gateway["volumes"])
    assert "default.conf.template:/etc/nginx/templates/default.conf.template:ro" in mounts
    assert "15-jt-cors-map.sh:/docker-entrypoint.d/15-jt-cors-map.sh:ro" in mounts
    assert gateway["environment"]["NGINX_ENVSUBST_FILTER"] == "^SUPABASE_GATEWAY_"
    assert gateway["environment"]["SUPABASE_CORS_ORIGINS"] == "${SUPABASE_CORS_ORIGINS:-${SUPABASE_SITE_URL}}"


def test_public_listener_closes_postgrest_and_the_auth_admin_api():
    public, internal = _server(8000), _server(8001)

    for prefix in ("^~ /auth/v1/admin", "^~ /rest/v1/"):
        assert "return 404" in _location(public, prefix)
    assert "proxy_pass http://supabase-rest:3000/" not in public
    assert "proxy_pass http://supabase-rest:3000/" in internal
    assert "proxy_pass http://supabase-auth:9999/" in internal


def test_every_auth_route_overwrites_the_client_ip_header_and_the_public_one_is_rate_limited():
    public_auth = _location(_server(8000), "/auth/v1/")
    internal_auth = _location(_server(8001), "/auth/v1/")

    for block in (public_auth, internal_auth):
        assert "proxy_set_header X-JT-Client-IP $remote_addr;" in block
    assert "limit_req zone=jt_auth" in public_auth
    assert "limit_req" not in internal_auth
    assert re.search(
        r"limit_req_zone \$jt_auth_limit_key zone=jt_auth:\S+ rate=\$\{SUPABASE_GATEWAY_AUTH_RATE\};",
        TEMPLATE,
    )
    limited = re.search(r"~\^/auth/v1/\(([^)]+)\)", TEMPLATE).group(1).split("|")
    assert {"token", "signup", "recover", "otp", "verify", "resend", "magiclink"} <= set(limited)


def test_real_client_ip_is_trusted_only_from_private_proxies():
    assert "real_ip_header X-Forwarded-For;" in TEMPLATE
    trusted = re.findall(r"set_real_ip_from (\S+);", TEMPLATE)
    assert "0.0.0.0/0" not in trusted
    assert set(trusted) == {"127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}


def test_cors_is_an_allowlist_not_a_wildcard():
    public = _server(8000)

    assert 'Access-Control-Allow-Origin "*"' not in TEMPLATE
    assert "add_header Access-Control-Allow-Origin $jt_cors_origin always;" in public
    assert "add_header Vary Origin always;" in public


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_cors_map_script_keeps_only_valid_exact_origins(tmp_path):
    out = tmp_path / "cors.conf"
    origins = 'http://localhost:5173, https://app.example.com/,evil.example,https://x";},ftp://y,'
    subprocess.run(
        ["sh", str(GATEWAY / "15-jt-cors-map.sh")],
        env={"PATH": "/usr/bin:/bin", "SUPABASE_CORS_ORIGINS": origins, "JT_CORS_MAP_FILE": str(out)},
        check=True,
        capture_output=True,
    )
    text = out.read_text()

    entries = re.findall(r'^\s+"([^"]+)" "\1";$', text, re.M)
    assert entries == ["http://localhost:5173", "https://app.example.com"]
    assert text.count("{") == 1 and text.count("}") == 1
    assert 'default "";' in text


def test_cors_map_script_is_executable():
    assert (GATEWAY / "15-jt-cors-map.sh").stat().st_mode & 0o111
