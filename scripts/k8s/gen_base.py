"""Generate infra/k8s/base and infra/k8s/components (committed output).

python scripts/k8s/gen_base.py   # from the repository root
"""

import pathlib
import sys

import yaml

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "infra/k8s/base")
APP = "jt-code"
TLS_SECRET_NAME = "jt-code-tls"  # cert-manager writes the certificate here  # pragma: allowlist secret


def labels(component):
    return {"app.kubernetes.io/name": APP, "app.kubernetes.io/component": component}


POD_SECURITY = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "runAsGroup": 10001,
    "fsGroup": 10001,
    "seccompProfile": {"type": "RuntimeDefault"},
}
CONTAINER_SECURITY = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "privileged": False,
    "capabilities": {"drop": ["ALL"]},
}
COMMON_ENV = [
    {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
    {"name": "PROMETHEUS_MULTIPROC_DIR", "value": "/tmp/prometheus"},
]
ENV_FROM = [{"configMapRef": {"name": "jt-code-config"}}, {"secretRef": {"name": "jt-code-secrets"}}]


def volumes(scratch):
    vols = [{"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]
    mounts = [{"name": "tmp", "mountPath": "/tmp"}]
    for name in scratch:
        vols.append({"name": name.replace("_", "-"), "emptyDir": {"sizeLimit": "2Gi"}})
        mounts.append({"name": name.replace("_", "-"), "mountPath": f"/app/{name}"})
    return vols, mounts


def deployment(
    name,
    component,
    *,
    args,
    image="jt-code-api",
    replicas=1,
    resources,
    env=(),
    ports=(),
    probes=None,
    grace=60,
    scratch=(),
    strategy=None,
    spread=False,
):
    vols, mounts = volumes(scratch)
    container = {
        "name": component,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "args": args,
        "envFrom": ENV_FROM,
        "env": [*COMMON_ENV, *env],
        "resources": resources,
        "securityContext": CONTAINER_SECURITY,
        "volumeMounts": mounts,
    }
    if ports:
        container["ports"] = list(ports)
    container.update(probes or {})
    pod = {
        "serviceAccountName": "jt-code",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "terminationGracePeriodSeconds": grace,
        "securityContext": POD_SECURITY,
        "containers": [container],
        "volumes": vols,
    }
    if spread:
        pod["topologySpreadConstraints"] = [
            {
                "maxSkew": 1,
                "topologyKey": "kubernetes.io/hostname",
                "whenUnsatisfiable": "ScheduleAnyway",
                "labelSelector": {"matchLabels": labels(component)},
            }
        ]
    spec = {
        "replicas": replicas,
        "revisionHistoryLimit": 5,
        "selector": {"matchLabels": labels(component)},
        "template": {
            "metadata": {
                "labels": labels(component),
                "annotations": {"kubectl.kubernetes.io/default-container": component},
            },
            "spec": pod,
        },
    }
    spec["strategy"] = strategy or {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": labels(component)},
        "spec": spec,
    }


def res(cpu_req, mem_req, cpu_lim, mem_lim):
    return {"requests": {"cpu": cpu_req, "memory": mem_req}, "limits": {"cpu": cpu_lim, "memory": mem_lim}}


def http_probe(path, *, period, failure, initial=0, timeout=5):
    probe = {
        "httpGet": {"path": path, "port": "http"},
        "periodSeconds": period,
        "timeoutSeconds": timeout,
        "failureThreshold": failure,
    }
    if initial:
        probe["initialDelaySeconds"] = initial
    return probe


WORKER_PROBE = {
    "livenessProbe": {
        "exec": {"command": ["sh", "-c", 'celery -A config inspect ping --timeout 10 -d "celery@$HOSTNAME"']},
        "initialDelaySeconds": 60,
        "periodSeconds": 120,
        "timeoutSeconds": 30,
        "failureThreshold": 3,
    }
}
WORKERS = {
    # name: (queues, concurrency, resources, grace, image, scratch, extra env)
    "worker-default": (
        "jobs.default,orchestration",
        "4",
        res("100m", "384Mi", "1", "768Mi"),
        620,
        "jt-code-api",
        (),
        [],
    ),
    "worker-ai": ("jobs.analysis", "8", res("200m", "512Mi", "2", "1Gi"), 620, "jt-code-api", (), []),
    "worker-ingestion": (
        "jobs.ingestion",
        "4",
        res("200m", "512Mi", "2", "1536Mi"),
        620,
        "jt-code-api",
        (),
        [],
    ),
    "worker-documents": (
        "jobs.visualization",
        "2",
        res("250m", "768Mi", "2", "2Gi"),
        620,
        "jt-code-api-office",
        ("converted_files", "rendered_documents", "generated_images"),
        [{"name": "HOME", "value": "/tmp"}],
    ),
    "worker-analytics": (
        "analytics.analysis,analytics.visualization",
        "2",
        res("500m", "2Gi", "2", "4Gi"),
        330,
        "jt-code-api",
        (),
        [],
    ),
}


def write(name, docs):
    text = yaml.safe_dump_all(docs, sort_keys=False)
    # A TLS secret *reference* is not a secret; tell detect-secrets so.
    text = text.replace("secretName: jt-code-tls\n", "secretName: jt-code-tls  # pragma: allowlist secret\n")
    with (OUT / name).open("w") as handle:
        handle.write("# Generated by scripts/k8s/gen_base.py - edit the generator, not this file.\n")
        handle.write(text)


resources_list = []

# Service account and shared config --------------------------------------------
write(
    "serviceaccount.yaml",
    [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "jt-code", "labels": labels("serviceaccount")},
            "automountServiceAccountToken": False,
            # Created per environment by infra/terraform (modules/app_environment).
            "imagePullSecrets": [{"name": "ghcr-pull"}],
        }
    ],
)
resources_list.append("serviceaccount.yaml")

# API ---------------------------------------------------------------------------
api = deployment(
    "jt-code-api",
    "api",
    args=["api"],
    replicas=2,
    spread=True,
    grace=30,
    resources=res("250m", "512Mi", "2", "1Gi"),
    env=[{"name": "WEB_CONCURRENCY", "value": "2"}],
    ports=[{"name": "http", "containerPort": 8000, "protocol": "TCP"}],
    scratch=("converted_files", "rendered_documents", "generated_images"),
    probes={
        "startupProbe": http_probe("/api/v1/health/startup/", period=5, failure=24),
        "livenessProbe": http_probe("/api/v1/health/live/", period=15, failure=3),
        "readinessProbe": http_probe("/api/v1/health/ready/", period=10, failure=3, timeout=8),
        # Let the endpoints controller and ingress drop the pod before uvicorn stops.
        "lifecycle": {"preStop": {"exec": {"command": ["sleep", "5"]}}},
    },
)
api_service = {
    "apiVersion": "v1",
    "kind": "Service",
    "metadata": {"name": "jt-code-api", "labels": labels("api")},
    "spec": {"selector": labels("api"), "ports": [{"name": "http", "port": 80, "targetPort": "http"}]},
}
api_pdb = {
    "apiVersion": "policy/v1",
    "kind": "PodDisruptionBudget",
    "metadata": {"name": "jt-code-api", "labels": labels("api")},
    "spec": {"minAvailable": 1, "selector": {"matchLabels": labels("api")}},
}
api_hpa = {
    "apiVersion": "autoscaling/v2",
    "kind": "HorizontalPodAutoscaler",
    "metadata": {"name": "jt-code-api", "labels": labels("api")},
    "spec": {
        "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "jt-code-api"},
        "minReplicas": 2,
        "maxReplicas": 10,
        "metrics": [
            {
                "type": "Resource",
                "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}},
            },
            {
                "type": "Resource",
                "resource": {"name": "memory", "target": {"type": "Utilization", "averageUtilization": 80}},
            },
        ],
        "behavior": {
            "scaleDown": {
                "stabilizationWindowSeconds": 300,
                "policies": [{"type": "Pods", "value": 1, "periodSeconds": 60}],
            },
            "scaleUp": {
                "stabilizationWindowSeconds": 0,
                "policies": [{"type": "Percent", "value": 100, "periodSeconds": 60}],
            },
        },
    },
}
write("api.yaml", [api, api_service, api_pdb, api_hpa])
resources_list.append("api.yaml")

# Workers -----------------------------------------------------------------------
for name, (queues, concurrency, resources, grace, image, scratch, extra) in WORKERS.items():
    component = name
    worker = deployment(
        f"jt-code-{name}",
        component,
        args=["worker"],
        image=image,
        grace=grace,
        resources=resources,
        env=[
            {"name": "CELERY_QUEUES", "value": queues},
            {"name": "CELERY_CONCURRENCY", "value": concurrency},
            {"name": "CELERY_METRICS_PORT", "value": "9808"},
            *extra,
        ],
        ports=[{"name": "metrics", "containerPort": 9808, "protocol": "TCP"}],
        scratch=scratch,
        probes=WORKER_PROBE,
    )
    pdb = {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": f"jt-code-{name}", "labels": labels(component)},
        "spec": {"maxUnavailable": 1, "selector": {"matchLabels": labels(component)}},
    }
    hpa = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": f"jt-code-{name}", "labels": labels(component)},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": f"jt-code-{name}"},
            "minReplicas": 1,
            "maxReplicas": 6,
            "metrics": [
                {
                    "type": "Resource",
                    "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 75}},
                }
            ],
            "behavior": {"scaleDown": {"stabilizationWindowSeconds": 600}},
        },
    }
    write(f"{name}.yaml", [worker, pdb, hpa])
    resources_list.append(f"{name}.yaml")

# Beat (exactly one) and Kafka consumers ------------------------------------------------
beat = deployment(
    "jt-code-beat",
    "beat",
    args=["beat"],
    grace=30,
    resources=res("50m", "192Mi", "500m", "384Mi"),
    strategy={"type": "Recreate"},
)
consumer = deployment(
    "jt-code-consumer-integrations",
    "consumer-integrations",
    args=["consumer", "integrations.webhook.received", "--consumer-name", "integration-webhooks"],
    grace=60,
    resources=res("50m", "256Mi", "500m", "512Mi"),
)
write("beat.yaml", [beat])
write("consumers.yaml", [consumer])
resources_list += ["beat.yaml", "consumers.yaml"]

# Streamlit -----------------------------------------------------------------------------
streamlit = deployment(
    "jt-code-streamlit",
    "streamlit",
    args=[],
    image="jt-code-streamlit",
    grace=30,
    resources=res("100m", "256Mi", "1", "1Gi"),
    ports=[{"name": "http", "containerPort": 8501, "protocol": "TCP"}],
    probes={
        "readinessProbe": http_probe("/_stcore/health", period=10, failure=3),
        "livenessProbe": http_probe("/_stcore/health", period=20, failure=3),
    },
)
container = streamlit["spec"]["template"]["spec"]["containers"][0]
container.pop("args")
container["envFrom"] = [{"configMapRef": {"name": "jt-code-streamlit-config"}}]
container["env"] = []
streamlit_service = {
    "apiVersion": "v1",
    "kind": "Service",
    "metadata": {"name": "jt-code-streamlit", "labels": labels("streamlit")},
    "spec": {"selector": labels("streamlit"), "ports": [{"name": "http", "port": 80, "targetPort": "http"}]},
}
write("streamlit.yaml", [streamlit, streamlit_service])
resources_list.append("streamlit.yaml")

# Migrations (run by the deploy pipeline before the rollout) -------------------------------
migrate_pod = deployment(
    "x", "migrate", args=["migrate"], grace=30, resources=res("100m", "384Mi", "1", "768Mi")
)
migrate_spec = migrate_pod["spec"]["template"]["spec"]
migrate_spec["restartPolicy"] = "Never"
migrate = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "jt-code-migrate", "labels": labels("migrate")},
    "spec": {
        "backoffLimit": 1,
        "activeDeadlineSeconds": 900,
        "ttlSecondsAfterFinished": 86400,
        "template": {"metadata": {"labels": labels("migrate")}, "spec": migrate_spec},
    },
}
write("migrate-job.yaml", [migrate])
resources_list.append("migrate-job.yaml")

# Network policies ------------------------------------------------------------------------
policies = [
    {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny-ingress"},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress"]},
    },
    {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "allow-ingress-controller"},
        "spec": {
            "podSelector": {
                "matchExpressions": [
                    {"key": "app.kubernetes.io/component", "operator": "In", "values": ["api", "streamlit"]}
                ]
            },
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}
                            }
                        }
                    ]
                }
            ],
        },
    },
    {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "allow-prometheus-scrape"},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": APP}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}}
                    ],
                    "ports": [{"port": 8000, "protocol": "TCP"}, {"port": 9808, "protocol": "TCP"}],
                }
            ],
        },
    },
    {
        # Egress stays open (Supabase, Redis, Kafka, AI providers, n8n, Stripe, ImageKit)
        # except the cloud metadata endpoint, a classic SSRF target.
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "deny-cloud-metadata"},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["169.254.169.254/32"]}}]}],
        },
    },
]
write("networkpolicies.yaml", policies)
resources_list.append("networkpolicies.yaml")

# Ingress --------------------------------------------------------------------------------
ingress = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "Ingress",
    "metadata": {
        "name": "jt-code",
        "labels": labels("ingress"),
        "annotations": {
            "cert-manager.io/cluster-issuer": "letsencrypt",
            "nginx.ingress.kubernetes.io/proxy-body-size": "30m",
            # Chat SSE streams last up to CHAT_SSE_MAX_SECONDS (300 s).
            "nginx.ingress.kubernetes.io/proxy-read-timeout": "320",
            "nginx.ingress.kubernetes.io/proxy-send-timeout": "320",
            "nginx.ingress.kubernetes.io/proxy-buffering": "off",
            "nginx.ingress.kubernetes.io/server-snippet": "location = /metrics { return 404; }",
        },
    },
    "spec": {
        "ingressClassName": "nginx",
        "tls": [{"hosts": ["api.example.com", "analytics.example.com"], "secretName": TLS_SECRET_NAME}],
        "rules": [
            {
                "host": "api.example.com",
                "http": {
                    "paths": [
                        {
                            "path": "/",
                            "pathType": "Prefix",
                            "backend": {"service": {"name": "jt-code-api", "port": {"name": "http"}}},
                        }
                    ]
                },
            },
            {
                "host": "analytics.example.com",
                "http": {
                    "paths": [
                        {
                            "path": "/",
                            "pathType": "Prefix",
                            "backend": {"service": {"name": "jt-code-streamlit", "port": {"name": "http"}}},
                        }
                    ]
                },
            },
        ],
    },
}
write("ingress.yaml", [ingress])
resources_list.append("ingress.yaml")

kustomization = {
    "apiVersion": "kustomize.config.k8s.io/v1beta1",
    "kind": "Kustomization",
    "labels": [{"pairs": {"app.kubernetes.io/part-of": APP}, "includeSelectors": False}],
    "resources": resources_list,
}
with (OUT / "kustomization.yaml").open("w") as handle:
    handle.write("# Generated by scripts/k8s/gen_base.py - edit the generator, not this file.\n")
    yaml.safe_dump(kustomization, handle, sort_keys=False)
print("ok", len(resources_list))

# Components ------------------------------------------------------------------------------
components = OUT.parent / "components"
(components / "keda").mkdir(parents=True, exist_ok=True)
(components / "monitoring").mkdir(parents=True, exist_ok=True)

# KEDA: scale Celery workers on Redis queue depth (the broker list per queue) instead of CPU.
keda_docs = [
    {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "TriggerAuthentication",
        "metadata": {"name": "jt-code-redis"},
        "spec": {
            "secretTargetRef": [
                {"parameter": "address", "name": "jt-code-secrets", "key": "KEDA_REDIS_ADDRESS"},
                {"parameter": "password", "name": "jt-code-secrets", "key": "KEDA_REDIS_PASSWORD"},
            ]
        },
    }
]
deletes = []
for name, (queues, *_rest) in WORKERS.items():
    keda_docs.append(
        {
            "apiVersion": "keda.sh/v1alpha1",
            "kind": "ScaledObject",
            "metadata": {"name": f"jt-code-{name}", "labels": labels(name)},
            "spec": {
                "scaleTargetRef": {"name": f"jt-code-{name}"},
                "minReplicaCount": 1,
                "maxReplicaCount": 10,
                "pollingInterval": 15,
                "cooldownPeriod": 300,
                "triggers": [
                    {
                        "type": "redis",
                        "authenticationRef": {"name": "jt-code-redis"},
                        "metadata": {
                            "listName": queue,
                            "listLength": "10",
                            "enableTLS": "true",
                            "databaseIndex": "1",
                        },
                    }
                    for queue in queues.split(",")
                ],
            },
        }
    )
    deletes.append(
        {
            "patch": (
                "$patch: delete\napiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\n"
                f"metadata:\n  name: jt-code-{name}\n"
            )
        }
    )
with (components / "keda" / "scaledobjects.yaml").open("w") as handle:
    handle.write("# Generated by scripts/k8s/gen_base.py - edit the generator, not this file.\n")
    yaml.safe_dump_all(keda_docs, handle, sort_keys=False)
with (components / "keda" / "kustomization.yaml").open("w") as handle:
    handle.write("# Generated by scripts/k8s/gen_base.py. Requires KEDA (installed by infra/terraform).\n")
    yaml.safe_dump(
        {
            "apiVersion": "kustomize.config.k8s.io/v1alpha1",
            "kind": "Component",
            "resources": ["scaledobjects.yaml"],
            "patches": deletes,
        },
        handle,
        sort_keys=False,
    )

# Monitoring (prometheus-operator): scrape targets and the alert rules from infra/prometheus.
alerts = yaml.safe_load((OUT.parent.parent / "prometheus" / "alerts.yml").read_text())
monitoring_docs = [
    {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {"name": "jt-code-api", "labels": {**labels("api"), "release": "kube-prometheus-stack"}},
        "spec": {
            "selector": {"matchLabels": labels("api")},
            "endpoints": [
                {
                    "port": "http",
                    "path": "/metrics",
                    "interval": "30s",
                    "authorization": {
                        "type": "Bearer",
                        "credentials": {"name": "jt-code-secrets", "key": "METRICS_AUTH_TOKEN"},
                    },
                }
            ],
        },
    },
    {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {
            "name": "jt-code-workers",
            "labels": {"app.kubernetes.io/name": APP, "release": "kube-prometheus-stack"},
        },
        "spec": {
            "selector": {
                "matchExpressions": [
                    {"key": "app.kubernetes.io/component", "operator": "In", "values": list(WORKERS)}
                ]
            },
            "podMetricsEndpoints": [{"port": "metrics", "interval": "30s"}],
        },
    },
    {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": "jt-code",
            "labels": {"app.kubernetes.io/name": APP, "release": "kube-prometheus-stack"},
        },
        "spec": alerts,
    },
]
with (components / "monitoring" / "monitors.yaml").open("w") as handle:
    handle.write("# Generated by scripts/k8s/gen_base.py from infra/prometheus/alerts.yml.\n")
    yaml.safe_dump_all(monitoring_docs, handle, sort_keys=False)
with (components / "monitoring" / "kustomization.yaml").open("w") as handle:
    handle.write(
        "# Generated by scripts/k8s/gen_base.py. Requires kube-prometheus-stack (infra/terraform).\n"
    )
    yaml.safe_dump(
        {
            "apiVersion": "kustomize.config.k8s.io/v1alpha1",
            "kind": "Component",
            "resources": ["monitors.yaml"],
        },
        handle,
        sort_keys=False,
    )
print("components ok")
