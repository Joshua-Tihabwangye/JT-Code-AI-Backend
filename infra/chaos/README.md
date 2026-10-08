# Chaos experiments (Phase 18)

These are [Chaos Mesh](https://chaos-mesh.org) experiments for **staging only**.
Each manifest starts with its hypothesis. Run one at a time while
`loadtests/load.json` provides steady traffic, and watch the Grafana dashboards.

| Experiment | Expected outcome (pass criteria) |
| --- | --- |
| `worker-pod-kill.yaml` | Every submitted job reaches a terminal state, with no duplicate credit settlement and no duplicate callback (check `jt_jobs_active` drains and `UsageRecord` uniqueness) |
| `beat-pod-kill.yaml` | One beat pod returns; sweeps resume; no reservation stays held past its TTL |
| `redis-network-loss.yaml` | API error rate < 1%; `RateLimitStoreUnavailable` fires; queued work completes after recovery |
| `provider-latency.yaml` | Fallback model used (`fallback_used` on ModelRun); chat p95 ≤ 15 s; no 5xx |
| `api-cpu-stress.yaml` | HPA scales out within 2 min; error rate < 1%; p95 recovers after the stress ends |

```bash
kubectl apply -f infra/chaos/redis-network-loss.yaml     # staging cluster only
# ... observe for the duration, then:
kubectl delete -f infra/chaos/redis-network-loss.yaml
```

Write the outcome as JSON (`{"passed": true, "experiment": "...", "observations": {...}}`)
and store it as release evidence:

```bash
kubectl -n jt-code-staging exec -i deploy/jt-code-api -- \
  python manage.py record_evidence chaos_experiment - < redis-loss-result.json
```

The same failure modes are also exercised in-process on every CI run by
`tests/test_phase18_verification.py`: Redis down, broker down, Kafka down,
provider outage with fallback, and an embedding outage.
