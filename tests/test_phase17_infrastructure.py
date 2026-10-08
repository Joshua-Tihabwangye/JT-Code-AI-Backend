"""Phase 17: infrastructure-as-code policy checks (no cluster or Docker needed).

The exit criterion - a reproducible staging deployment from clean
infrastructure - rests on these invariants, which CI enforces on every change:

* images are pinned, multi-stage and run as a non-root user;
* every Celery queue the code routes to is consumed by exactly one worker;
* every workload is hardened (non-root, read-only root FS, no capabilities,
  seccomp, resources) and the API has health probes, HPA and a PDB;
* each overlay's config plus the Terraform-managed secret keys satisfy the
  real staging/production settings validation (a deploy cannot boot half-configured);
* manifests are regenerated from the generator, deploys go by digest, and
  production is a gated promotion of signed images.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
K8S = ROOT / "infra" / "k8s"
OVERLAYS = ("staging", "production")


def _docs(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _base_objects() -> list[dict]:
    return [
        doc
        for path in sorted((K8S / "base").glob("*.yaml"))
        if path.name != "kustomization.yaml"
        for doc in _docs(path)
    ]


def _pod_specs() -> list[tuple[str, dict]]:
    specs = []
    for obj in _base_objects():
        if obj["kind"] in {"Deployment", "Job"}:
            specs.append((obj["metadata"]["name"], obj["spec"]["template"]["spec"]))
    return specs


def _env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


# Images ------------------------------------------------------------------------------


def test_dockerfiles_are_pinned_multistage_and_non_root():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"ARG PYTHON_IMAGE=python:[\d.]+-slim-\w+@sha256:[0-9a-f]{64}", dockerfile)
    assert "AS builder" in dockerfile and "COPY --from=builder /wheels" in dockerfile
    assert "build-essential" not in dockerfile.split("AS runtime", 1)[1]  # no compilers at runtime
    for stage in dockerfile.split("\nFROM ")[2:]:
        assert "USER 10001:10001" in stage, "every runtime stage must end as the non-root user"
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"' in dockerfile
    assert "ADD " not in dockerfile and "rm -rf /var/lib/apt/lists/*" in dockerfile
    streamlit = (ROOT / "streamlit_app" / "Dockerfile").read_text()
    assert re.search(r"@sha256:[0-9a-f]{64}", streamlit) and "USER 10001:10001" in streamlit
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    for secret in (".env", "**/*.tfvars", "**/*.tfstate*", ".git", "infra/n8n/n8n.env"):
        assert secret in ignored


def test_entrypoint_dispatches_every_role():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
    for role in ("api)", "worker)", "beat)", "consumer)", "migrate)", "n8n-push)", "manage)"):
        assert role in entrypoint
    assert "exec uvicorn config.asgi:application" in entrypoint  # ASGI: SSE streams
    assert "CELERY_QUEUES:?" in entrypoint  # a worker never silently consumes the default queue only
    assert "--no-server-header" in entrypoint


# Queues --------------------------------------------------------------------------------


def _declared_queues() -> set[str]:
    return {queue.name for queue in settings.CELERY_TASK_QUEUES}


def test_every_routed_queue_is_declared_and_consumed_exactly_once():
    routed = {route["queue"] for route in settings.CELERY_TASK_ROUTES.values()}
    routed |= {"orchestration"}  # apps.orchestration dispatches with an explicit queue
    declared = _declared_queues()
    assert routed <= declared, routed - declared

    consumed: dict[str, list[str]] = {}
    for obj in _base_objects():
        if obj["kind"] != "Deployment":
            continue
        container = obj["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container.get("env", [])}
        for queue in (env.get("CELERY_QUEUES") or "").split(","):
            if queue:
                consumed.setdefault(queue, []).append(obj["metadata"]["name"])
    assert set(consumed) == declared, declared ^ set(consumed)
    assert all(len(owners) == 1 for owners in consumed.values()), consumed

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    compose_queues = set()
    for service in compose["services"].values():
        queues = (service.get("environment") or {}).get("CELERY_QUEUES", "")
        compose_queues |= {queue for queue in queues.split(",") if queue}
    assert compose_queues == declared


# Kubernetes ------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "pod"), _pod_specs(), ids=[name for name, _ in _pod_specs()])
def test_every_workload_is_hardened(name, pod):
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 10001
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    for container in pod["containers"]:
        security = container["securityContext"]
        assert security["readOnlyRootFilesystem"] is True
        assert security["allowPrivilegeEscalation"] is False
        assert security["capabilities"] == {"drop": ["ALL"]}
        assert set(container["resources"]) == {"requests", "limits"}
        assert ":" not in container["image"], "base images are retagged by digest in overlays"
        mounts = {mount["mountPath"] for mount in container["volumeMounts"]}
        assert "/tmp" in mounts


def test_api_has_probes_autoscaling_and_disruption_budget():
    objects = {(obj["kind"], obj["metadata"]["name"]): obj for obj in _base_objects()}
    container = objects[("Deployment", "jt-code-api")]["spec"]["template"]["spec"]["containers"][0]
    assert container["startupProbe"]["httpGet"]["path"] == "/api/v1/health/startup/"
    assert container["livenessProbe"]["httpGet"]["path"] == "/api/v1/health/live/"
    assert container["readinessProbe"]["httpGet"]["path"] == "/api/v1/health/ready/"
    assert {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}} in container["env"]
    hpa = objects[("HorizontalPodAutoscaler", "jt-code-api")]
    assert hpa["spec"]["minReplicas"] >= 2
    assert objects[("PodDisruptionBudget", "jt-code-api")]["spec"]["minAvailable"] >= 1
    beat = objects[("Deployment", "jt-code-beat")]
    assert beat["spec"]["replicas"] == 1 and beat["spec"]["strategy"] == {"type": "Recreate"}
    ingress = objects[("Ingress", "jt-code")]
    assert "/metrics" in ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/server-snippet"]


def test_workers_autoscale_on_queue_depth_with_keda():
    workers = [
        obj["metadata"]["name"]
        for obj in _base_objects()
        if obj["kind"] == "Deployment" and obj["metadata"]["name"].startswith("jt-code-worker-")
    ]
    keda = _docs(K8S / "components" / "keda" / "scaledobjects.yaml")
    scaled = {obj["spec"]["scaleTargetRef"]["name"]: obj for obj in keda if obj["kind"] == "ScaledObject"}
    assert set(scaled) == set(workers)
    for obj in scaled.values():
        assert all(trigger["type"] == "redis" for trigger in obj["spec"]["triggers"])
    deleted = yaml.safe_load((K8S / "components" / "keda" / "kustomization.yaml").read_text())["patches"]
    assert len(deleted) == len(workers)  # CPU HPAs are replaced, never fighting KEDA


def test_network_policies_default_deny_and_block_cloud_metadata():
    policies = {p["metadata"]["name"]: p for p in _docs(K8S / "base" / "networkpolicies.yaml")}
    assert policies["default-deny-ingress"]["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}
    egress = policies["deny-cloud-metadata"]["spec"]["egress"][0]["to"][0]["ipBlock"]
    assert "169.254.169.254/32" in egress["except"]


def test_generated_manifests_are_up_to_date(tmp_path):
    shadow = tmp_path / "infra"
    (shadow / "k8s" / "base").mkdir(parents=True)
    (shadow / "prometheus").mkdir()
    (shadow / "prometheus" / "alerts.yml").write_text((ROOT / "infra/prometheus/alerts.yml").read_text())
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/k8s/gen_base.py"), str(shadow / "k8s" / "base")],
        check=True,
        capture_output=True,
    )
    for generated in sorted((shadow / "k8s").rglob("*.yaml")):
        committed = K8S / generated.relative_to(shadow / "k8s")
        assert committed.read_text() == generated.read_text(), f"regenerate {committed} (make k8s-generate)"


# Environments ------------------------------------------------------------------------------

_DUMMY = {
    "DJANGO_SECRET_KEY": "k" * 20 + "0123456789abcdefghijklmnopqrstuvwxyzABCD",
    "DATABASE_URL": "postgresql://postgres.ref@aws-0-eu-central-1.pooler.supabase.com:6543/postgres?sslmode=require",
    "SUPABASE_URL": "https://ref.supabase.co",
    "SUPABASE_JWKS_URL": "https://ref.supabase.co/auth/v1/.well-known/jwks.json",
    "SUPABASE_JWT_ISSUER": "https://ref.supabase.co/auth/v1",
    "SUPABASE_JWT_AUDIENCE": "authenticated",
    "SUPABASE_SECRET_KEY": "sb_secret_0123456789abcdef",  # pragma: allowlist secret
    "REDIS_URL": "rediss://redis.example.net:6380/0",
    "CELERY_BROKER_URL": "rediss://redis.example.net:6380/1",
    "CELERY_RESULT_BACKEND": "rediss://redis.example.net:6380/2",
    "KAFKA_BOOTSTRAP_SERVERS": "kafka.example.net:9093",
    "SUPABASE_STORAGE_BUCKET": "jt-code-assets",
    "STRIPE_SECRET_KEY": "sk_live_0123456789abcdef",  # pragma: allowlist secret
    "STRIPE_WEBHOOK_SECRET": "whsec_0123456789abcdef",  # pragma: allowlist secret
    "TOOL_CREDENTIALS_ENCRYPTION_KEYS": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "SENTRY_DSN": "https://public@o1.ingest.sentry.io/1",
}


@pytest.mark.parametrize("environment", OVERLAYS)
def test_overlay_config_and_secret_keys_pass_settings_validation(environment, monkeypatch):
    from config.settings.validation import validate_environment

    config = _env_file(K8S / "overlays" / environment / "config.env")
    secret_keys = [
        line.strip()
        for line in (K8S / "secret-keys.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert not set(config) & set(secret_keys), "a key is either config or secret, never both"
    assert config["DJANGO_SETTINGS_MODULE"] == f"config.settings.{environment}"
    assert config["API_HOST"] == config["DJANGO_ALLOWED_HOSTS"]
    assert config["N8N_CALLBACK_BASE_URL"] == f"https://{config['API_HOST']}/api/v1"

    for key in list(os.environ):
        if key.isupper() and not key.startswith(("PATH", "HOME", "PYTHON", "PYTEST", "VIRTUAL_ENV")):
            monkeypatch.delenv(key, raising=False)
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    for key in secret_keys:
        monkeypatch.setenv(key, _DUMMY.get(key, f"{key.lower()}-0123456789abcdefghijklmnop"))
    assert validate_environment(environment) == []


def test_terraform_owns_namespace_secret_and_validates_required_keys():
    app = (ROOT / "infra/terraform/modules/app_environment/main.tf").read_text()
    assert '"pod-security.kubernetes.io/enforce" = "restricted"' in app
    assert "precondition" in app and "missing_keys" in app
    environment = (ROOT / "infra/terraform/modules/environment/main.tf").read_text()
    for generated in ("DJANGO_SECRET_KEY", "N8N_DISPATCH_SECRET", "N8N_WEBHOOK_SECRET", "METRICS_AUTH_TOKEN"):
        assert f'"{generated}"' in environment
    staging = (ROOT / "infra/terraform/envs/staging/main.tf").read_text()
    production = (ROOT / "infra/terraform/envs/production/main.tf").read_text()
    assert "manage_zone_policy               = false" in staging  # one owner per Cloudflare zone
    assert "manage_zone_policy               = true" in production
    for root in (staging, production):
        assert 'backend "s3" {}' in root  # remote state, configured per environment
    assert not list((ROOT / "infra/terraform").rglob("*.tfvars")), "tfvars must never be committed"


def test_local_compose_runs_a_self_hosted_supabase_runtime():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    assert services["supabase-db"]["image"].startswith("supabase/postgres:")
    assert "supabase-auth" in services
    assert "supabase-storage" in services
    assert "supabase-gateway" in services
    assert services["n8n-db"]["image"].startswith("postgres:")
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} must not listen on all interfaces"
    assert services["worker"]["env_file"] == [{"path": ".env", "required": False}]
    # Compose expands variables before a service's env_file is loaded. These
    # startup-critical passwords must therefore have Compose-level defaults.
    supabase_password = services["supabase-db"]["environment"]["POSTGRES_PASSWORD"]
    n8n_password = services["n8n-db"]["environment"]["POSTGRES_PASSWORD"]
    assert "${JT_CODE_LOCAL_SUPABASE_DB_PASSWORD:-" in supabase_password
    assert "${JT_CODE_LOCAL_N8N_DB_PASSWORD:-" in n8n_password
    storage_probe = services["supabase-storage"]["healthcheck"]["test"]
    assert "http://127.0.0.1:5000/status" in storage_probe


def test_local_compose_launcher_lists_and_opens_browser_endpoints():
    launcher = (ROOT / "scripts/compose_up.sh").read_text()
    for endpoint in (
        "http://127.0.0.1:8000/api/docs/",
        "http://127.0.0.1:54321/auth/v1/health",
        "http://127.0.0.1:5678",
        "http://127.0.0.1:8025",
        "http://127.0.0.1:8501",
        "http://127.0.0.1:9090",
        "http://127.0.0.1:3000",
    ):
        assert endpoint in launcher
    assert "up --build --detach" in launcher
    assert "google-chrome" in launcher
    assert "AUTO_OPEN_BROWSER" in launcher
    makefile = (ROOT / "Makefile").read_text()
    assert "compose-up-no-browser:" in makefile
    assert "bash scripts/compose_up.sh" in makefile


def test_container_entrypoint_prepares_prometheus_directory_before_manage_commands():
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text()
    assert entrypoint.index("prepare_metrics_dir\n\ncase") < entrypoint.index("  manage)")


# CI/CD ---------------------------------------------------------------------------------------


def test_ci_runs_every_gate():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert {"quality", "test", "security", "image", "manifests", "terraform", "workflows"} <= set(ci["jobs"])
    image_steps = " ".join(str(step) for step in ci["jobs"]["image"]["steps"])
    assert "trivy" in image_steps and "'exit-code': '1'" in image_steps and "hadolint" in image_steps
    assert "--entrypoint id" in image_steps and "10001" in image_steps  # images run as non-root


def test_deploys_are_by_digest_signed_and_production_is_gated():
    deploy = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    jobs = deploy["jobs"]
    build_steps = " ".join(str(step) for step in jobs["build"]["steps"])
    assert "cosign sign" in build_steps and "'sbom': True" in build_steps
    assert jobs["staging"]["environment"]["name"] == "staging"
    promote = jobs["promote"]
    assert promote["if"] == "github.event_name == 'workflow_dispatch'"
    assert "environment" in promote
    assert "cosign verify" in " ".join(str(step) for step in promote["steps"])
    script = (ROOT / "scripts/deploy/deploy.sh").read_text()
    assert "deploy by digest only" in script
    assert script.index("job/jt-code-migrate") < script.index("rollout status")  # migrate before rollout
    assert "rollout undo" in script  # failed smoke tests roll back
