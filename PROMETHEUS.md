# Prometheus Config Changes — OpenObserve Observability Rollout

All changes below are to `k8s/observability/config/prometheus.yml`, in the order they were
actually made. At the time, the config was a plain ConfigMap, so every
`remote_write`/`scrape_configs` change required a manual
`kubectl rollout restart deployment/prometheus -n observability` to take effect. It no longer
does: `k8s/observability/kustomization.yaml` now generates the ConfigMap with `configMapGenerator`,
which puts a content hash in its name, so an edit changes the pod template and Argo CD rolls the
Prometheus pod by itself.

The snippets in steps 1-4 show the credentials as they were then (a literal `username`). The
current config reads both from the `openobserve-remote-write-credentials` Secret via
`username_file`/`password_file` - see the final state at the end.

## 1. Added `remote_write` to OpenObserve (unscoped — this broke)

First cut: forward every scraped series to OpenObserve's Prometheus-remote-write-compatible
endpoint, no filtering.

```yaml
remote_write:
  - url: http://openobserve.observability.svc.cluster.local:5080/api/default/prometheus/api/v1/write
    basic_auth:
      username: root@example.com
      password_file: /etc/prometheus/openobserve-auth/password
```

**Result (confirmed live):** every write request failed with HTTP 400
`{"code":400,"message":"Error# MemoryTableOverflowError"}`. Every distinct Prometheus metric name
becomes its own OpenObserve stream, and forwarding every job — in particular
`kubernetes-nodes-cadvisor`'s per-container, per-node series — created far more concurrent streams
than OpenObserve's single-node in-memory MemTable (`config.ZO_MEM_TABLE_MAX_SIZE`, `"0"` = auto-sized,
in `k8s/observability/openobserve-values.yaml`) could hold.

## 2. Scoped remote_write to just the API's own metrics

Added `write_relabel_configs` to keep only the `otel-collector` job (the message-service's own
FastAPI/SQLAlchemy metrics, pushed via OTLP — see `app/telemetry.py`) before it reaches
OpenObserve:

```yaml
remote_write:
  - url: http://openobserve.observability.svc.cluster.local:5080/api/default/prometheus/api/v1/write
    basic_auth:
      username: root@example.com
      password_file: /etc/prometheus/openobserve-auth/password
    write_relabel_configs:
      - source_labels: [job]
        regex: 'otel-collector'
        action: keep
```

Also required restarting the OpenObserve pod itself (`kubectl delete pod -n observability
openobserve-0`) to clear the stuck stream backlog left over from step 1 — the fix alone wasn't
enough to unstick an already-overflowed instance.

**Result (confirmed live):** every write request returned HTTP 200, `prometheus_remote_storage_samples_failed_total` stopped climbing.

## 3. Widened scope to add cluster ops metrics (`node-exporter`, `kube-state-metrics`)

Requested: forward the same cluster-ops metrics the existing "Cluster ops" Grafana dashboard
already uses. Added these two jobs to the keep regex — done first, before the higher-cardinality
`kubernetes-nodes-cadvisor`, to verify no overflow incrementally. (OpenObserve's `resources` were
bumped in `openobserve-values.yaml` in the same step, in anticipation of the added volume.)

```yaml
write_relabel_configs:
  - source_labels: [job]
    regex: 'otel-collector|node-exporter|kube-state-metrics'
    action: keep
```

**Result (confirmed live):** ~51,660 samples sent, 0 failures.

## 4. Widened scope further to add `kubernetes-nodes-cadvisor`

The highest-cardinality job (per-container, per-node) — the one that caused the original overflow
in step 1 — added last and watched closely:

```yaml
write_relabel_configs:
  - source_labels: [job]
    regex: 'otel-collector|node-exporter|kube-state-metrics|kubernetes-nodes-cadvisor'
    action: keep
```

**Result (confirmed live):** ~244,000 samples sent, 0 failures, over a sustained multi-minute
window; `prometheus_remote_storage_shards_desired` stayed at ~0.03 (nowhere near falling behind);
OpenObserve pod had 0 restarts. Confirmed real cAdvisor data landed (`container_memory_working_set_bytes`
queryable in OpenObserve).

This is the config's current state (`k8s/observability/config/prometheus.yml` lines 24–32).

## 5. Added a new scrape job for OpenObserve's own metrics (self-monitoring)

Separate from remote_write — this feeds the new "OpenObserve Ops" Grafana dashboard by scraping
OpenObserve's own `/metrics` endpoint locally. **Not** added to the `write_relabel_configs` keep
list above — no reason to remote_write OpenObserve's self-metrics back into itself.

```yaml
- job_name: 'openobserve'
  static_configs:
    - targets: ['openobserve.observability.svc.cluster.local:5080']
```

Required `config.ZO_PROMETHEUS_ENABLED: "true"` in `openobserve-values.yaml` first — off by
default, confirmed live that `/metrics` returns HTTP 200 with an empty body otherwise.

## Current `remote_write` + relevant `scrape_configs` (final state)

```yaml
remote_write:
  - url: http://openobserve.observability.svc.cluster.local:5080/api/default/prometheus/api/v1/write
    basic_auth:
      username_file: /etc/prometheus/openobserve-auth/username
      password_file: /etc/prometheus/openobserve-auth/password
    write_relabel_configs:
      - source_labels: [job]
        regex: 'otel-collector|node-exporter|kube-state-metrics|kubernetes-nodes-cadvisor'
        action: keep

scrape_configs:
  - job_name: 'otel-collector'
    static_configs:
      - targets: ['otel-collector.observability.svc.cluster.local:8889']

  - job_name: 'kube-state-metrics'
    static_configs:
      - targets: ['kube-state-metrics.observability.svc.cluster.local:8080']

  - job_name: 'openobserve'
    static_configs:
      - targets: ['openobserve.observability.svc.cluster.local:5080']

  # ... postgres-exporter, hazelcast, node-exporter and kubernetes-nodes-cadvisor pre-existed
  # this work and are unchanged; trivy-operator was added later - see config/prometheus.yml
  # for the full file.
```

## Service discovery: where it's used, and why not more (reviewed 2026-09-28)

**Used for two jobs only.** `node-exporter` (`kubernetes_sd_configs` role `pod`, limited to the
`observability` namespace and pods labelled `app=node-exporter`) and `kubernetes-nodes-cadvisor`
(role `node`, scraped through the API server proxy). Both follow the nodes, which the cluster
autoscaler adds and removes. The other seven jobs (`otel-collector`, `kube-state-metrics`,
`openobserve`, `postgres-exporter`, `hazelcast`, `trivy-operator`) use `static_configs` pointing at
a Service DNS name. There are no `ServiceMonitor`/`PodMonitor` objects (this is plain Prometheus,
not Prometheus Operator), no `prometheus.io/*` annotation-based job, and GKE Managed Prometheus is
off (`--no-enable-managed-prometheus` in `gke-deploy.sh`).

**Why static targets are fine as things stand:**

- Every statically scraped target runs **one replica**. Scraping a Service name only goes wrong
  when several pods sit behind it: each scrape reaches a random pod, so counters jump between
  pods' values.
- The API (3-6 pods under the HPA) is never scraped. It pushes over OTLP to the collector, and
  each series carries `k8s_pod_name`, so scaling it changes nothing here.
- Discovery wouldn't make adding a target automatic: `default` and `observability` are
  default-deny ingress, so a new target needs a NetworkPolicy rule letting Prometheus in anyway.

**Not recommended:** Prometheus Operator (CRDs plus an operator pod on three `e2-standard-2`
nodes, and a rewrite of a working setup for `ServiceMonitor`s), or re-enabling GKE Managed
Prometheus (off on purpose - this repo runs its own).

**When to add discovery:**

1. **`otel-collector` scaled past one replica.** Switch that job to `kubernetes_sd_configs` with
   `role: endpoints`, kept to the `otel-collector` Service, so every collector pod is scraped
   separately. This is the one change that would otherwise break existing metrics.
2. **Scraping platform components** (Argo CD, Kyverno, Crossplane, External Secrets - all expose
   metrics nothing scrapes today). Add one annotation-based job (`role: pod`, keep
   `prometheus.io/scrape: "true"`, port/path from `prometheus.io/port`/`prometheus.io/path`), plus
   an ingress rule per namespace for Prometheus. Leave these jobs out of the `remote_write` keep
   list above - OpenObserve's MemTable is what overflowed last time.

**RBAC note:** `prometheus-rbac.yaml` also grants `services`, `endpoints` and `ingresses`, which no
current job uses. `endpoints` becomes necessary with case 1; the rest can go.
