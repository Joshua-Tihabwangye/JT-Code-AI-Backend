"""Edge proxy, deployable frontend image and API load balancing (backlog Phase 4)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = Path(os.environ.get("JT_FRONTEND_DIR", ROOT.parent / "jt-code-frontend"))
CADDYFILE = ROOT / "docker/edge/Caddyfile"
NATIVE_CADDY = Path.home() / ".local/share/jt-code-native/bin/caddy"


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def _caddy() -> str | None:
    return shutil.which("caddy") or (str(NATIVE_CADDY) if NATIVE_CADDY.exists() else None)


def _frontend(name: str) -> str:
    path = FRONTEND / name
    if not path.exists():
        pytest.skip("frontend repository not checked out next to the backend")
    return path.read_text()


# compose -------------------------------------------------------------------------------------


def test_edge_and_web_run_only_with_the_edge_profile():
    services = _compose()["services"]

    assert services["edge"]["profiles"] == ["edge"]
    assert services["web"]["profiles"] == ["edge"]
    unprofiled = [name for name, svc in services.items() if "profiles" not in svc]
    assert "api" in unprofiled and "edge" not in unprofiled


def test_only_the_edge_publishes_ports_beyond_loopback():
    services = _compose()["services"]
    public = {
        name: port
        for name, svc in services.items()
        for port in svc.get("ports", [])
        if not str(port).startswith("127.0.0.1:")
    }

    assert set(public) == {"edge"}
    assert set(services["edge"]["ports"]) == {"80:80", "443:443", "443:443/udp"}


def test_api_scales_horizontally_on_one_host():
    api = _compose()["services"]["api"]

    assert api["deploy"]["replicas"] == "${API_REPLICAS:-1}"
    assert api["ports"] == ["127.0.0.1:${API_HOST_PORTS:-8000}:8000"]


def test_web_image_is_built_in_api_mode_from_the_server_settings():
    web = _compose()["services"]["web"]
    args = web["build"]["args"]

    assert args["VITE_DATA_MODE"] == "api"
    assert args["VITE_SUPABASE_URL"] == "${SUPABASE_PUBLIC_URL}"
    assert args["VITE_SUPABASE_ANON_KEY"] == "${SUPABASE_ANON_KEY}"
    assert args["VITE_API_BASE_URL"] == "/api/v1"
    assert web["environment"]["JT_SUPABASE_ORIGIN"] == "${SUPABASE_PUBLIC_URL}"
    assert web["cap_drop"] == ["ALL"]


def test_prometheus_discovers_every_api_and_worker_replica():
    scrape = yaml.safe_load((ROOT / "infra/prometheus/single-host.yml").read_text())["scrape_configs"]
    jobs = {job["job_name"]: job for job in scrape}

    assert jobs["jt-code-api"]["dns_sd_configs"][0]["names"] == ["api"]
    assert set(jobs["jt-code-workers"]["dns_sd_configs"][0]["names"]) == {"worker", "worker-documents"}


# Caddyfile -----------------------------------------------------------------------------------


def test_edge_closes_operator_paths_and_load_balances_the_api():
    text = CADDYFILE.read_text()
    internal = re.search(r"@internal path (.+)", text).group(1).split()

    for path in ("/admin/*", "/metrics", "/api/docs/*", "/api/v1/schema/*", "/rest/v1/*", "/auth/v1/admin/*"):
        assert path in internal or path in text.split("@internal path", 2)[2]
    assert "dynamic a {" in text and "name api" in text
    assert "lb_policy least_conn" in text
    assert "flush_interval -1" in text
    assert "reverse_proxy web:8080" in text
    assert "reverse_proxy supabase-gateway:8000" in text
    assert "Strict-Transport-Security" in text


@pytest.mark.skipif(_caddy() is None, reason="caddy binary not available")
def test_caddyfile_is_valid_and_routes_both_domains():
    env = {**os.environ, "APP_DOMAIN": "app.example.com", "SUPABASE_DOMAIN": "supabase.example.com"}
    adapted = subprocess.run(
        [_caddy(), "adapt", "--config", str(CADDYFILE)], env=env, capture_output=True, text=True, check=True
    )
    config = json.loads(adapted.stdout)
    hosts = {
        host
        for server in config["apps"]["http"]["servers"].values()
        for route in server["routes"]
        for match in route.get("match", [])
        for host in match.get("host", [])
    }

    assert {"app.example.com", "supabase.example.com"} <= hosts
    assert "admin" not in config or config["admin"].get("disabled") is True


# Frontend image ------------------------------------------------------------------------------


def test_frontend_dockerfile_builds_api_mode_and_fails_without_supabase_settings():
    dockerfile = _frontend("Dockerfile")

    assert "CLERK" not in dockerfile
    assert "ARG VITE_DATA_MODE=api" in dockerfile
    assert "VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY are required" in dockerfile
    assert "nginx-unprivileged" in dockerfile
    ignored = _frontend(".dockerignore").splitlines()
    assert ".env.*" in ignored and ".env" in ignored


def test_frontend_nginx_sends_a_strict_csp_and_cache_headers():
    nginx = _frontend("nginx.conf")
    headers = _frontend("nginx-security-headers.inc")
    csp = re.search(r'set \$jt_csp "([^"]+)"', nginx).group(1)
    directives = {part.split()[0]: part.split()[1:] for part in csp.split("; ")}

    assert directives["script-src"] == ["'self'", "https://js.stripe.com"]
    assert "'unsafe-eval'" not in csp and "'unsafe-inline'" not in directives["script-src"]
    assert directives["object-src"] == ["'none'"]
    assert directives["frame-ancestors"] == ["$jt_frame_ancestors"]
    assert "${JT_SUPABASE_ORIGIN}" in directives["connect-src"]
    assert '~^/embed/ "*";' in nginx and "default \"'none'\";" in nginx
    assert 'Cache-Control "public, max-age=31536000, immutable"' in nginx
    assert 'Cache-Control "no-cache"' in nginx
    assert "listen 8080;" in nginx and "proxy_pass" not in nginx
    assert "add_header Content-Security-Policy $jt_csp always;" in headers
