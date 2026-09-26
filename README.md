# Message Service (FastAPI + SQLAlchemy + PostgreSQL on Kubernetes)

A REST API (Python 3.13, [FastAPI](https://fastapi.tiangolo.com/)) for `Message` and `Author`
resources, persisting through [SQLAlchemy](https://www.sqlalchemy.org/) 2.0 (async) against a
PostgreSQL database, with a Hazelcast read-through cache, deployed to a local
[kind](https://kind.sigs.k8s.io/) cluster. The REST surface is documented in
[API-DESIGN.md](API-DESIGN.md) and, when `API_DOCS_ENABLED=true`, served as OpenAPI at `/docs`.

Everything below the application layer - PostgreSQL, the standalone Hazelcast cache member, the kind
cluster topology, ingress, Argo CD, Kyverno and the full observability stack - is the platform this
service runs on. [NEW-PROJECT-BLUEPRINT.md](NEW-PROJECT-BLUEPRINT.md) describes the contracts an
application must satisfy for that platform to keep working.

## Stack URLs

Once `deploy-kind.sh` completes, the stack is reachable at (`*.localhost` resolves to `127.0.0.1`
on macOS/most Linux out of the box - see [Deployment with Kind / Kubernetes](#deployment-with-kind--kubernetes)):

| Component | URL | Notes |
| --- | --- | --- |
| message-service REST API | `http://localhost/messages`, `http://localhost/authors` | see [REST API](#rest-api) |
| message-service API docs | `http://localhost/docs` | OpenAPI UI; only while `API_DOCS_ENABLED=true` (it is, in this dev cluster) |
| message-service health | `http://localhost/health/liveness` | |
| Grafana | `http://grafana.localhost/` | credentials via Secret / 1Password - see [Viewing metrics in Grafana](#viewing-metrics-in-grafana) |
| OpenObserve | `http://openobserve.localhost/` | credentials via Secret / 1Password |
| Headlamp | `http://headlamp.localhost/` | Kubernetes dashboard; login needs a bearer token, see [Headlamp](#viewing-metrics-in-grafana) |
| ArgoCD | `http://argocd.localhost/` | GitOps sync UI; login is `admin` / see [Continuous Deployment with ArgoCD](#continuous-deployment-with-argocd) |
| Prometheus | `http://localhost:9090` | not exposed via Ingress - `kubectl port-forward -n observability svc/prometheus 9090:9090` |


## Architecture

- **API**: FastAPI app (`app/main.py`) -> thin routers (`app/routers/`) -> service layer
  (`app/services/`: optimistic locking, cache-aside) -> SQLAlchemy async ORM (`app/models.py`,
  `app/db.py`), served by uvicorn on `:8080`, one worker per pod (the HPA scales out). Pydantic v2
  models (`app/schemas.py`) validate requests; every error is RFC 9457 `application/problem+json`
  (`app/errors.py`).
- **Data model**: `Author` (id, name, email) has many `Message`s (id, title, content, `version` for
  optimistic locking, `author` relation) - see [app/models.py](app/models.py). Alembic migrations
  (`migrations/`) create the schema; `alembic upgrade head` runs on every container start, guarded
  by a Postgres advisory lock so three replicas starting together don't race (see the Dockerfile's
  `ENTRYPOINT` and `migrations/env.py`).

- **Cluster topology** (`k8s/kind-config.yaml`): 1 control-plane + 6 workers.
  - 2 workers labeled `workload=api` — the `message-service` Deployment (3 replicas) is pinned there
    via `nodeSelector`, with preferred pod anti-affinity so replicas spread across those nodes. A
    `HorizontalPodAutoscaler` (`k8s/hpa.yaml`) keeps it between 3 and 6 replicas, scaling on CPU
    (70% average utilization) and memory (80%) against the container's `resources.requests` in
    `k8s/deployment.yaml`. It reads usage from **metrics-server**, which `deploy-kind.sh` installs
    with `--kubelet-insecure-tls` (kind's kubelet serving certs aren't signed for metrics-server's
    default verification).
  - 1 worker labeled `workload=db` — the `postgres` StatefulSet (1 replica, with a `PersistentVolumeClaim`)
    is pinned there via `nodeSelector`.
  - 1 worker labeled `workload=observability` — the OTel Collector, Prometheus, and Grafana
    Deployments (see below) are pinned there via `nodeSelector`.
  - 1 worker labeled `workload=cache` — the `hazelcast` Deployment (see below) is pinned there
    via `nodeSelector`.
  - 1 worker labeled `workload=openobserve` — the OpenObserve `StatefulSet` (see below) is pinned
    there via `nodeSelector`.
  - [Headlamp](https://headlamp.dev/) (a general-purpose Kubernetes web UI, own `headlamp`
    namespace) is pinned to the `workload=observability` node too, alongside Grafana - it's a
    lightweight single-pod dashboard with no metrics-pipeline role of its own, so it doesn't
    warrant a dedicated node.
- **Lookup cache**: `GET /messages/{id}` reads through a `messages` map on a standalone
  [Hazelcast](https://github.com/hazelcast/hazelcast) member (`k8s/hazelcast-deployment.yaml`,
  `k8s/hazelcast-service.yaml`) that each `message-service` pod connects to as a **client**
  (`app/cache.py`) rather than embedding a member per pod - that keeps the cache independent of app
  pod restarts/scaling. `PATCH`/`DELETE /messages/{id}` evict the entry to keep the shared cache
  correct (see [Concurrency & Transaction Isolation](#concurrency--transaction-isolation) for the
  one extra eviction that heals a stale entry).


  There's deliberately no client-side Near Cache here: it was tried (in the original Java version of
  this app) and removed after verifying, in a real multi-pod deployment, that it went stale across
  pods after an evict - a pod other than the one that wrote an update kept serving old data
  indefinitely, since the client SDK's cross-client near-cache invalidation broadcast wasn't reliably
  reaching the other pod's near-cache. Reads go straight to the shared Hazelcast member instead,
  which stays correct - see `app/cache.py`'s comments. Connecting to Hazelcast is not optional: same
  as the DB connection, a missing/unreachable member fails startup rather than silently running
  without a cache.
- **Ingress**: the control-plane node is labeled `ingress-ready=true` and maps host ports 80/443
  (see [kind's Ingress guide](https://kind.sigs.k8s.io/docs/user/ingress/)). `deploy-kind.sh` installs
  the ingress-nginx controller, and `k8s/ingress.yaml` routes all paths to `message-service`
  (a plain `ClusterIP` Service - no NodePort). The API is reachable at `http://localhost/messages`
  with no port number and no `kubectl port-forward` needed. The API is served at `/`, so there's no
  rewrite rule; a service added behind a path prefix would need its own `Ingress` (the
  `rewrite-target` annotation applies to a whole Ingress object) and FastAPI's `root_path` set to
  that prefix so the generated OpenAPI links are right.

- **Observability** (`k8s/observability/`, namespace `observability`): the app pushes metrics as OTLP
  (`opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http`, see `app/telemetry.py`)
  to an **OpenTelemetry Collector** (`otel-collector`, `otel/opentelemetry-collector-contrib`), which
  re-exposes them in Prometheus format on port 8889. **Prometheus** scrapes the collector, and
  **Grafana** (provisioned with that Prometheus datasource and pre-built dashboards - request
  rate/latency by route, event-loop lag, process memory, DB connection-pool stats) is exposed via
  `k8s/observability/ingress.yaml` at `http://grafana.localhost/` (credentials configured via Secrets /
  1Password, see `k8s/observability/grafana-secret.yaml`). `*.localhost` resolves to `127.0.0.1` on
  modern OSes/browsers without any `/etc/hosts` change.


  **OpenObserve** (`openobserve/openobserve-standalone` Helm chart - single-node, not the HA chart;
  installed by `deploy-kind.sh`, values in `k8s/observability/openobserve-values.yaml`) is a second,
  independent observability backend, fed by Prometheus `remote_write`. It's exposed at
  `http://openobserve.localhost/` (credentials configured via Secrets / 1Password, see
  `k8s/observability/openobserve-values.yaml` and `k8s/observability/openobserve-prometheus-secret.yaml`
  - the latter is what Prometheus itself authenticates with, kept out of its ConfigMap on principle
  even though this is all disposable local-kind-only). Query its data under the `default` org, stream
  names matching the Prometheus metric names (e.g. `http_requests_total`,
  `container_memory_working_set_bytes`).

  **[Headlamp](https://headlamp.dev/)** (`headlamp/headlamp` Helm chart, own `headlamp` namespace -
  a general-purpose Kubernetes web UI, not part of the message-service metrics pipeline above;
  installed by `deploy-kind.sh`, values in `k8s/headlamp/headlamp-values.yaml`) gives a
  browse/inspect/edit view over every resource in the cluster (pods, deployments, logs, exec,
  node status, etc.), which is a different job than Grafana/OpenObserve's time-series metrics. It's
  exposed at `http://headlamp.localhost/`. No OIDC is configured, so the login page needs a bearer
  token; the chart's default `ClusterRoleBinding` grants its own `headlamp` ServiceAccount
  `cluster-admin`, so the simplest local token is `kubectl create token headlamp -n headlamp
  --duration=24h` (that's cluster-admin in the browser - fine for this disposable local kind
  cluster only, not a pattern to reuse anywhere shared).

  Headlamp's image bundles the official
  [Prometheus plugin](https://github.com/headlamp-k8s/plugins/tree/main/prometheus)
  (`config.staticPlugins.enabled: true` in `k8s/headlamp/headlamp-values.yaml`, the chart default),
  which adds metrics charts to workload detail pages. It auto-detects Prometheus in-cluster via the
  `headlamp-prometheus: "true"` label on `k8s/observability/prometheus-service.yaml` - no extra
  per-cluster config needed.

  `write_relabel_configs` in `k8s/observability/config/prometheus.yml` deliberately keeps only
  four scrape jobs - `otel-collector` (the message-service's own metrics), plus `node-exporter`,
  `kube-state-metrics`, and `kubernetes-nodes-cadvisor` (the same three jobs behind the "kind cluster
  ops" Grafana dashboard) - not every job Prometheus scrapes. See `PROMETHEUS.md` for why (an earlier
  attempt at forwarding everything unfiltered overflowed OpenObserve's single-node in-memory MemTable).

## Continuous Deployment with ArgoCD

The API is deployed via GitOps rather than the local build/load loop described in
[Pushing a code change to the running cluster](#pushing-a-code-change-to-the-running-cluster):
GitHub Actions builds and pushes images to Docker Hub, and [ArgoCD](https://argo-cd.readthedocs.io/)
(`deploy-kind.sh` installs it into its own `argocd` namespace, exposed at `http://argocd.localhost/`)
plus [Argo CD Image Updater](https://argocd-image-updater.readthedocs.io/) take it from there.

- **CI** (`.github/workflows/message-service-ci.yml`): on push to `main` (and on pull requests, up
  to the test gate), path-filtered to `app/**`, `migrations/**`, `pyproject.toml`, `uv.lock` and the
  `Dockerfile`, the workflow runs `ruff check`, `pyright` and `pytest` (against a Postgres service
  container) as a gate, then builds and pushes `docker.io/miqui/rest-message-api`, tagged
  `<UTC yyyymmddHHMMSS>-<7-char sha>` (e.g. `20260918140501-a1b2c3d`, sortable by build time yet
  traceable to a commit) plus a floating `:latest`. Images are built for both `linux/amd64` and
  `linux/arm64` (via QEMU): kind's nodes run the host's architecture (arm64 on Apple Silicon), and an
  amd64-only image fails to pull there with `no match for platform in manifest`. Push credentials
  (`DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN`, a **read/write** Docker Hub access token, not the account
  password) are GitHub Actions repository secrets, never committed.
- **ArgoCD** owns one `Application` (`k8s/argocd/application.yaml`, `fastapi-o2`) whose source is
  this repo's `k8s/` Kustomization - the Postgres/Hazelcast/message-service/Ingress/ResourceQuota set
  `kubectl apply -k k8s/` would apply directly. `syncPolicy.automated: { prune: true, selfHeal: true }`
  means any manifest change pushed to `main` (or drift corrected by hand in the live cluster) gets
  reconciled automatically. This repo is public, so ArgoCD clones it anonymously - no repository
  credential is needed. The three Applications' `repoURL` is
  `https://github.com/miqui/k8s-fastapi-o2.git`: that repository must exist, with these manifests on
  `main`, before `deploy-kind.sh` runs.

- **Observability** is a second `Application` (`k8s/argocd/observability-application.yaml`, `observability`)
  syncing `k8s/observability/` into the `observability` namespace, with the same automated prune/self-heal.
  A merged dashboard, scrape-config or collector-config change therefore reaches the cluster through Argo
  instead of a hand-run `kubectl apply`. Prometheus, the OTel Collector and the log collector read their
  config once at startup, so their ConfigMaps are generated by kustomize (`configMapGenerator`, sources
  in `k8s/observability/config/`): the content hash in the name rolls the pod whenever the config changes.
  The namespace carries `Prune=false` so it can't be deleted by removing it from git (OpenObserve's PVC
  lives there). OpenObserve and Headlamp are Helm installs and stay outside it. See
  [`ARGOCD.md`](ARGOCD.md) for the details and the one-time bootstrap on an existing cluster.
- **Argo CD Image Updater** (v1.x, pinned to `v1.3.0` in `deploy-kind.sh`) is configured by an
  `ImageUpdater` custom resource (`k8s/argocd/image-updater.yaml`) - v1.x replaced v0.x's
  Application annotations with this CRD. It watches `docker.io/miqui/rest-message-api`, considers only tags matching `^[0-9]{14}-[0-9a-f]{7}$` (so never the
  floating `:latest`), and picks the highest one with the `alphabetical` strategy - i.e. the newest
  build, given the timestamp-prefixed tags. `newest-build` would be the obvious strategy but its
  docs advise against it on Docker Hub: it fetches a manifest per tag to read creation dates, and
  those count against pull limits. It polls with a **separate, read-only** Docker Hub token
  (`DOCKERHUB_TOKEN_RO`, also from 1Password) so a compromised in-cluster credential can't push or
  delete images. On finding a new tag it patches the `Application`'s Kustomize image override
  directly (the default `argocd` write-back method, equivalent to `kustomize edit set image
  message-service=docker.io/miqui/rest-message-api:<tag>` - `manifestTargets.kustomize.name` maps
  the Deployment specs' short image name onto it) - no git commits, so CI and Image Updater never
  need push access to the GitHub repo at all.
- **The credential Secrets are the deliberate exception.** (`postgres-credentials`; the observability
  Application handles `grafana-credentials` and `openobserve-remote-write-credentials` the same way.) `k8s/secret.yaml` is a
  committed placeholder (`YOUR_POSTGRES_DB_USER`, etc. - see its own comment); `deploy-kind.sh`
  overwrites it in-cluster with real 1Password-sourced values right after the `Application`'s
  first sync. Two settings in `application.yaml` keep ArgoCD from reverting it to the placeholder:
  `ignoreDifferences` on that Secret (so it isn't flagged as drift) **and** the
  `RespectIgnoreDifferences=true` sync option. Both are needed: `ignoreDifferences` alone only
  suppresses drift *detection*, while every sync - including the one Image Updater triggers when it
  changes an image - still applies the full manifest and would overwrite the real Secret, which
  breaks Postgres auth for any pod that starts afterward (`password authentication failed for
  user "YOUR_POSTGRES_DB_USER"`). This was hit for real on the first rollout.
- **Login**: `admin` / `kubectl -n argocd get secret argocd-initial-admin-secret -o
  jsonpath='{.data.password}' | base64 -d` (same bearer-token-retrieval idiom as Headlamp above).
  `argocd-server` is patched with `--insecure` so the plain-HTTP `*.localhost` Ingress pattern used
  for Grafana/OpenObserve/Headlamp works here too, rather than needing TLS passthrough.

## Policy as Code with Kyverno

[Kyverno](https://kyverno.io/) admission-controls what may run in the cluster, and the *same*
policies are checked against the manifests in CI before a change merges. They use Kyverno's
CEL-based `policies.kyverno.io/v1` types (`ValidatingPolicy`, `PolicyException`); the older
`ClusterPolicy` is deprecated (Kyverno's docs schedule its removal for v1.20), so don't copy
examples that use it.

- **Delivery**: `deploy-kind.sh` installs Kyverno itself with Helm (pinned, values in
  `k8s/kyverno/kyverno-values.yaml`); the policies are a separate ArgoCD `Application`
  (`kyverno-policies`) syncing `k8s/policies/` with `prune` + `selfHeal`, so a rule deleted from git
  stops being enforced and a policy edited or deleted by hand is put back.
- **Layout** (`k8s/policies/`): `rules/` holds each rule body once, unscoped. Two kustomize overlays
  turn them into a **Deny** copy for the `default` namespace (`overlays/enforce-default`, names get
  an `-enforce` suffix) and an **Audit** copy for `observability` and `headlamp`
  (`overlays/audit-other`, `-audit`) - the same rule, enforced where this repo owns and has hardened
  the workloads, report-only where it doesn't yet. `audit-only/` holds rules that only ever audit,
  and `exceptions/` the `PolicyException`s.

| Policy | What it checks | Mode |
| --- | --- | --- |
| `disallow-host-access` | no privileged containers, `hostNetwork`/`hostPID`/`hostIPC`, `hostPath` volumes or `hostPort`s (a Pod Security Standards "baseline" subset) | Deny in `default`, Audit in `observability`/`headlamp` |
| `require-secure-container-context` | every container, init containers included: `runAsNonRoot`, `allowPrivilegeEscalation: false`, drop `ALL` capabilities, `RuntimeDefault`/`Localhost` seccomp (a "restricted" subset; pod-level values count as defaults) | same |
| `require-resources` | CPU and memory requests **and** limits on every container | same |
| `restrict-image-repositories` | image must be on an exact-repository allowlist (tags/digests ignored; `postgres:16-alpine` is normalized to `docker.io/library/postgres`) | same |
| `disallow-latest-tag` | image pinned to a tag other than `:latest`, or a digest | Audit only |
| `restrict-cluster-admin-bindings` | `ClusterRoleBinding`s to `cluster-admin` (built-in `system:`/`kubeadm:` ones skipped) | Audit only, cluster-wide |

Rules that match Pods also cover Deployments, StatefulSets and DaemonSets (Kyverno "autogen"), so a
non-compliant Deployment is rejected when ArgoCD applies it rather than stalling later as
ReplicaSet events. `disallow-latest-tag` is audit-only on purpose: `k8s/kustomization.yaml`'s
`newTag: latest` is the fallback for the very first sync, before Image Updater swaps in a timestamped
tag, so denying it would block the first rollout.

**Hardened workloads.** To pass the enforce set, the `default`-namespace workloads now set a
`securityContext`: message-service runs as uid/gid 10001 with a read-only root filesystem (the image's numeric `app` user - the kubelet can only verify
`runAsNonRoot` for a numeric uid, so `runAsUser` is set explicitly as well), postgres
as 70 (its exporter sidecar as 65534) and hazelcast as 100:101, all with privilege escalation off and
all capabilities dropped. A Postgres volume first initialized by the old root-started container is
already owned by uid 70, so nothing needs migrating - but the securityContext change rolls
`postgres-0`, which means a short database outage for the API the first time it syncs.

**Exceptions** (`k8s/policies/exceptions/`) record known, accepted violations: `node-exporter` (it
needs `hostNetwork`/`hostPID`, a `hostPath` mount of `/` and a `hostPort` to read node metrics) is
exempt from the host-access and container-context audit rules, and the Headlamp chart's
`headlamp-admin` `cluster-admin` binding from `restrict-cluster-admin-bindings`. They live in the
`kyverno` namespace because that is the only place `features.policyExceptions` honours them. To add
one, create the file, list it in that directory's `kustomization.yaml`, and reference the
**suffixed** policy name in `policyRefs` (e.g. `require-secure-container-context-audit`).

### Checking manifests before merge

`.github/workflows/policy-check.yml` runs `check-policies.sh` on pull requests and on pushes to
`main` that touch `k8s/`. Run it locally the same way (needs `kubectl` and the Kyverno CLI, e.g.
`brew install kyverno`):

```bash
./check-policies.sh
```

1. The **enforce** policies against `k8s/` must pass - this is the blocking part.
2. The **audit** policies against `k8s/` and `k8s/observability/` are only reported (in the job
   summary in CI): today that is the `:latest` fallback and the four `observability` Deployments
   that still have no `securityContext`.
3. A **self-test** requires the enforce policies to reject `.github/policy-fixtures/violating-deployment.yaml`,
   and the run fails if no rules loaded, a policy errors, or nothing passed - otherwise a policy that
   silently stopped matching would let the check pass vacuously.

Enforce and audit are separate runs because the Kyverno CLI exits 1 on any failure and its
`--audit-warn` flag doesn't tell Deny from Audit for the CEL policy types. `PolicyException`s are
passed with `--exception`: in the same file as the policies the CLI loads nothing. CI installs the
CLI pinned to `v1.19.1` (SHA-256-checked) to match the Helm chart; bump the two together.

### Working with the policies

A `kubectl` cheat sheet for debugging denials, audit findings and Kyverno itself is in
[KYVERNO.md](KYVERNO.md).

- **Reports**: audit findings, and exceptions applied, appear in Kyverno's policy reports
  (`kubectl get policyreport -A`, plus `kubectl get clusterpolicyreport` for cluster-scoped ones).
- **New image**: add its repository to `allowed` in `k8s/policies/rules/restrict-image-repositories.yaml`
  (one list serves both the enforce and audit copies), or the enforce set will reject it.
- **New namespace / widening enforcement**: change the `namespaceSelector` in the overlays' patches.
  The webhook additionally never sees `kube-system`, `argocd`, `ingress-nginx` or
  `local-path-storage` (`config.webhooks` in `kyverno-values.yaml`), so an unhealthy Kyverno can't
  block system or GitOps changes.
- **Promoting an audit policy to enforce**: for a namespaced rule like `disallow-latest-tag`, fix
  what it flags first, then move it from `audit-only/` into `rules/` (and drop its own
  `namespaceSelector` - the overlays add one) so it gets Deny/Audit copies like the others; any
  exception's `policyRefs` then need the `-enforce`/`-audit` suffix. `restrict-cluster-admin-bindings`
  is cluster-scoped, so it stays where it is. Confirm with `./check-policies.sh`.

### Status and limits

- Kyverno v1.19 is tested against Kubernetes 1.33-1.35; the kind nodes here run a newer version.
  That is accepted rather than pinned - if the policies misbehave after a kind upgrade, suspect this
  first, and check `kubectl get pods -n kyverno` after the first deploy.
- `restrict-image-repositories` is exact-match on the *spelling*: `index.docker.io/...` is rejected
  even though it is Docker Hub. Only images in this repo's manifests are checked in CI - the
  Headlamp and OpenObserve charts are not rendered, so their pods are covered by the in-cluster audit
  policies only.
- The `observability` workloads are report-only until they get their own `securityContext`s.
- **Fail-closed on `default`.** The enforce policies keep Kyverno's default `failurePolicy: Fail`, and
  Kyverno runs one replica here, so while it is down (a restart, or all kind nodes coming back up at
  once) pod creation in `default` is rejected and retries until Kyverno is back. The audit policies
  set `failurePolicy: Ignore` - they can never deny, so an outage must not block anything on their
  account. Excluded namespaces (see above) are unaffected either way.
- **Reports need RBAC.** Kyverno's reports controller can only scan kinds the chart granted it;
  `reportsController.rbac.clusterRole.extraResources` in `kyverno-values.yaml` adds
  `clusterrolebindings` for `restrict-cluster-admin-bindings`. A policy on any other kind the chart
  doesn't cover needs the same, or it audits at admission time but produces no background results.
  Only the reports controller needs it. The policy's `ready` status is recomputed only when the
  policy object is reconciled, so after fixing RBAC on a running cluster re-apply or touch the policy
  (`kubectl annotate validatingpolicy <name> touched=$(date +%s) --overwrite`) or it keeps saying
  "missing permissions". A fresh install doesn't hit this: the grant is in the Helm values, so it
  already exists when the policies are first created.

## Vulnerability Scanning with Trivy Operator

[Trivy Operator](https://github.com/aquasecurity/trivy-operator) continuously scans every workload's
images (CVEs, baked-in secrets) and the cluster's resources (misconfigurations, RBAC, CIS/NSA/PSS
compliance), storing results as `VulnerabilityReport`, `ConfigAuditReport` etc. objects. It's
installed by its own Argo CD Application (`k8s/argocd/trivy-operator-application.yaml`: upstream
Helm chart plus `k8s/trivy-operator/trivy-operator-values.yaml` from this repo) into `trivy-system`.
Prometheus scrapes its metrics and the **Trivy Security** Grafana dashboard
(`http://grafana.localhost/d/trivy-security`) summarises them by severity, namespace and image.

It only reports; Kyverno is what enforces. Setup choices, `kubectl` commands for drilling into
individual CVEs and findings, and the dashboard's panels are in [TRIVY.md](TRIVY.md).

## Git history

The local `.git` directory was carried over on purpose, as background reference: `git log` /
`git show` against earlier commits still show the previous implementations of this service (the
`java-origin` remote points at the original project).

## Running the Application

### 1. Local run against PostgreSQL + Hazelcast

Requires [uv](https://docs.astral.sh/uv/) (it installs the Python version pinned in
`.python-version` for you). Start a local PostgreSQL instance (or reuse the one deployed in kind -
see below), plus a local Hazelcast member for the cache, and point the app at both:

```bash
docker run --rm -d --name message-postgres \
  -e POSTGRES_DB=messagedb -e POSTGRES_USER=message_app -e POSTGRES_PASSWORD=message_app \
  -p 5432:5432 postgres:16-alpine

docker run --rm -d --name message-hazelcast \
  -e HZ_CLUSTERNAME=message-service-cache \
  -p 5701:5701 hazelcast/hazelcast:5.7.0-slim

uv sync
uv run alembic upgrade head
API_DOCS_ENABLED=true uv run python -m app
```

*For iterative development with auto-reload:
`uv run uvicorn app.main:create_app --factory --reload` (skips the graceful-shutdown wrapper in
`app/__main__.py`, which only matters under Kubernetes).*

Configuration is environment variables (see `app/settings.py`): `DB_HOST`, `DB_PORT`, `DB_NAME`,
`DB_USER`, `DB_PASSWORD` - defaults connect to `localhost:5432/messagedb` with
`message_app`/`message_app`; `HAZELCAST_HOST`/`HAZELCAST_PORT` (default `localhost:5701`);
`OTEL_METRICS_URL`/`OTEL_TRACES_URL`; `API_DOCS_ENABLED` (exactly `"true"` or `"false"`, default
`false`; anything else fails startup); `CORS_ALLOWED_ORIGINS`; `PORT` (default `8080`). If you skip
starting a local Hazelcast member, the app will fail to start (the client connection is required, not
optional) - that's deliberate, matching how the DB connection behaves.

The API is at `http://localhost:8080` (`/messages`, `/authors`; interactive docs at
`http://localhost:8080/docs` with `API_DOCS_ENABLED=true`). Liveness/readiness probes are at
`/health/liveness` and `/health/readiness`.

---

### 2. Checks (lint, types, tests)

```bash
uv run ruff check
uv run pyright
uv run pytest
```

`pytest` runs against a real Postgres, never the dev `messagedb`: by default a `messagedb_test`
database on `localhost:5432` (override with the same `DB_*` variables the service reads). The
schema is created by running the real Alembic migration, so the migration is tested too; Hazelcast is
replaced by an in-memory fake. With the Postgres container above running:

```bash
docker exec message-postgres psql -U message_app -d messagedb -c 'CREATE DATABASE messagedb_test'
```

---

### 3. Building the image

```bash
docker build -t message-service:local .
docker run --rm --entrypoint id message-service:local   # uid=10001(app) gid=10001(app)
```

Multi-stage build: `uv sync --frozen` from `uv.lock` into a venv, copied into a slim runtime image
that runs as uid/gid 10001. The entrypoint is `alembic upgrade head && exec python -m app`, so the
server is PID 1 and receives `SIGTERM` directly.

---


## Deployment with Kind / Kubernetes

- **Deploy to local Kind cluster**: requires the [1Password CLI](https://developer.1password.com/docs/cli/) (`op`),
  installed and signed in (`eval $(op signin)`) - `deploy-kind.sh` checks both and fails fast otherwise, since
  PostgreSQL/Grafana/OpenObserve/Docker Hub credentials all come from it and there's no valid fallback.
    ```bash
    cp .env.example .env
    # Edit .env with your op://<vault>/<item>/<field> URIs
    op run --env-file=.env -- ./deploy-kind.sh
    ```
  - Creates a 7-node kind cluster (1 control-plane, 2 API workers, 1 DB worker, 1 observability
    worker, 1 cache worker, 1 OpenObserve worker) if it doesn't exist yet.
  - Installs the ingress-nginx controller and waits for it to become ready.
  - Installs metrics-server (patched with `--kubelet-insecure-tls`) and waits for it to become ready -
    required for the `message-service` `HorizontalPodAutoscaler` to read CPU/memory usage.
  - Installs ArgoCD and Argo CD Image Updater into the `argocd` namespace, configures Image
    Updater's read-only Docker Hub credentials, and exposes the ArgoCD UI at
    `http://argocd.localhost/` - see [Continuous Deployment with ArgoCD](#continuous-deployment-with-argocd).
    The `message-service` image is not built or `kind load`-ed locally; it comes from Docker Hub,
    built and pushed by GitHub Actions on push to `main`.
  - Installs [Kyverno](#policy-as-code-with-kyverno) via Helm (`kyverno/kyverno`, chart `3.9.1` =
    Kyverno v1.19.1, values in `k8s/kyverno/kyverno-values.yaml`) into its own `kyverno` namespace,
    before the observability stack, and waits for its `ValidatingPolicy`/`PolicyException` CRDs.
  - Registers the `observability` ArgoCD `Application` (`k8s/argocd/observability-application.yaml`, syncing
    `k8s/observability/`: OTel Collector, Prometheus, Grafana - see Architecture above) and waits for its first
    sync - it tracks `main`, so `k8s/observability/` has to be merged before you run the script - then
    dynamically injects observability secrets from environment, then
    installs OpenObserve via Helm (`openobserve/openobserve-standalone` - see Architecture above)
    and Headlamp via Helm (`headlamp/headlamp` - see Architecture above).
  - Registers the `kyverno-policies` ArgoCD `Application` (`k8s/argocd/policies-application.yaml`,
    syncing `k8s/policies/`) and waits for its first sync - *before* the app `Application` below,
    so the enforce policies already exist when the workloads first sync. It tracks `main`, so
    `k8s/policies/` has to be merged before you run the script.
  - Registers the `trivy-operator` ArgoCD `Application` (`k8s/argocd/trivy-operator-application.yaml`:
    the upstream `aqua/trivy-operator` Helm chart with `k8s/trivy-operator/trivy-operator-values.yaml`
    from `main`) and waits for its first sync; scan results keep arriving for minutes after. See
    [TRIVY.md](TRIVY.md).
  - Registers the ArgoCD `Application` that owns `k8s/` (replacing a direct `kubectl apply -k
    k8s/`): `Secret` + `ConfigMap`s, the `postgres` `StatefulSet`/headless `Service` (which creates
    `messagedb` via `POSTGRES_DB`), the `hazelcast` `Deployment`/`Service`, and the `message-service`
    `Deployment`/`Service` (`ClusterIP`)/`HorizontalPodAutoscaler`/`Ingress`. Waits for the
    `Application`'s first sync, then dynamically injects the real PostgreSQL database credentials
    from environment (overwriting the placeholder `k8s/secret.yaml` ArgoCD just synced).
  - Waits for PostgreSQL and Hazelcast to become ready before waiting on the API's rollout (the
    `message-service` Deployment runs `wait-for-postgres` and `wait-for-hazelcast` init containers).
- **Tear down cluster**: `./teardown-kind.sh`
- **Test endpoints**: `./test-api.sh` (exits non-zero on any failed check; `BASE_URL=... ./test-api.sh` to
  point it elsewhere)

### Pushing a code change to the running cluster

Code changes don't go through a local `docker build`/`kind load`/`rollout restart` loop - see
[Continuous Deployment with ArgoCD](#continuous-deployment-with-argocd) above. The flow is:

1. `git push` to `main` (a change under `app/**`, `migrations/**`, `pyproject.toml`, `uv.lock` or the
   `Dockerfile`) triggers `.github/workflows/message-service-ci.yml`, which lints, type-checks, tests,
   then builds and pushes a new commit-SHA-tagged image to Docker Hub.
2. Argo CD Image Updater (polling every 2 minutes by default) notices the new tag and updates the
   ArgoCD `Application`'s image override.
3. ArgoCD syncs the change, and `kubectl rollout status deployment/message-service` shows the rolling
   update happening.

This also runs `alembic upgrade head` again on every pod start (see the Dockerfile's `ENTRYPOINT`) -
idempotent, so a code-only change is a no-op there; a schema change picks up its new migration
automatically, and with three pods starting at once the advisory lock in `migrations/env.py` makes
exactly one of them apply it.

**This is a slower inner loop than a local run** (CI time + up to one Image Updater poll interval,
vs. seconds) - it's the deploy step, not the "test my change" step. For fast iteration, keep using
`uv run uvicorn app.main:create_app --factory --reload` against a local Postgres/Hazelcast, as
described in [Local run against PostgreSQL + Hazelcast](#1-local-run-against-postgresql--hazelcast)
above, and only push to `main` once you're ready to deploy.

A change to a manifest itself (env vars, resources, the Ingress, a new Kustomize resource, etc.)
under `k8s/` doesn't need a CI push at all - ArgoCD's own `selfHeal`/polling picks it up directly
from git the next time it reconciles (or immediately via the ArgoCD UI/CLI's manual "Sync" if you
don't want to wait). The cluster/node topology (`k8s/kind-config.yaml`) is still unmanaged by
ArgoCD - a `kind-config.yaml` change still needs [recreating the cluster](#adding-a-new-kind-node).


`deploy-kind.sh` already points your current `kubectl` context at the cluster
(`kubectl config use-context kind-kind-fastapi-cluster`), so no extra kubeconfig setup is
needed for the commands above. If you want a standalone `kubeconfig.yml` for this cluster instead —
e.g. to hand to another tool, or to talk to it without touching your default `~/.kube/config` context —
generate one with:

```bash
kind get kubeconfig --name kind-fastapi-cluster > kubeconfig.yml
export KUBECONFIG=./kubeconfig.yml   # use it for the current shell
kubectl get nodes -L workload
```

`kind get kubeconfig` always regenerates the file from the cluster's current certs, so re-run it if the
cluster is ever torn down and recreated. Don't commit `kubeconfig.yml` — it embeds client certificates
that grant full cluster-admin access to this kind cluster (`.gitignore` already excludes it by name).

> The credentials in `k8s/secret.yaml` are plaintext defaults meant only for this disposable local
> kind cluster. Do not reuse them, and manage real secrets with a proper secrets manager in any
> shared or production environment.

### Adding a new kind node

Unlike the code-push flow above, node topology **cannot** be changed on a running kind cluster —
kind has no "add node" command, since each node's kubeadm role is fixed at `kind create cluster`
time via `k8s/kind-config.yaml`. Adding one means recreating the cluster.

1. **Add the new worker to `k8s/kind-config.yaml`**, matching the existing API workers' pattern:
   ```yaml
     - role: worker
       kubeadmConfigPatches:
         - |
           kind: JoinConfiguration
           nodeRegistration:
             kubeletExtraArgs:
               node-labels: "workload=api"
   ```

2. **Recreate the cluster** with the updated config:
   ```bash
   kind delete cluster --name kind-fastapi-cluster
   kind create cluster --name kind-fastapi-cluster --config k8s/kind-config.yaml
   ```
   This wipes all cluster state (PostgreSQL data, any messages created only at runtime) - it's a
   fresh cluster, rebuilt from the manifests in `k8s/`.

3. **Bump the API replica count** in `k8s/deployment.yaml` (`spec.replicas: 3` -> `4`). The
   Deployment's `nodeSelector: workload: api` already targets any node with that label; it's the
   extra replica - combined with the existing `podAntiAffinity` spread by `kubernetes.io/hostname`
   - that actually lands a pod on the new node instead of just adding another pod to an
   already-occupied one.

4. **Redeploy** - since the cluster is new, `deploy-kind.sh` detects it doesn't exist yet and
   recreates it from step 1's config, then reapplies every manifest including the new replica count:
   ```bash
   ./deploy-kind.sh
   ```

5. **Verify** the new node exists and is running the API:
   ```bash
   kubectl get nodes -L workload -o wide
   kubectl get pods -l app=message-service -o wide
   ```
   Confirm a `message-service` pod's `NODE` column shows the new worker.

### Viewing metrics in Grafana

`deploy-kind.sh` also applies `k8s/observability/` (a separate Kustomization, in its own
`observability` namespace) and waits for it to roll out. Once deployed:

- **Grafana**: `http://grafana.localhost/` — log in with credentials configured via Secrets / 1Password (see
  `k8s/observability/grafana-secret.yaml`) and open one of eight
  pre-provisioned dashboards, all in `k8s/observability/grafana-dashboard-json-configmap.yaml` as plain
  PromQL against real, verified metric names - if you add new panels, check the exact metric names
  Prometheus actually stores first (they differ from the raw OTLP names — see below):
  - **API RED & Saturation**: the caller's-eye view of the API - request
    rate/latency percentiles/errors by route, DB connection-pool saturation, CPU throttling,
    memory vs. limit, restarts/readiness - see [API RED & Saturation Dashboard](#api-red--saturation-dashboard) below.
  - **message-service API**: app-level request rate/latency by route, event-loop lag, process memory,
    DB connection-pool stats.
  - **HTTP Operations** / **HTTP Errors**: traffic, latency and errors per HTTP method and route
    template, and per problem+json `code` - see
    [HTTP Operation & Error Dashboards](#http-operation--error-dashboards) below.

  `api-red`, `message-service-api`, `http-operations` and `http-errors` share a **Service** dropdown
  (`message-service`) - every series is filtered on the `service_name` label, so a second API pushing
  identically named metrics through the same collector would simply show up there as another option.

  - **kind cluster ops**: cluster-wide node/pod health - nodes ready, pod phases/restarts, per-node
    CPU/memory/disk (via node-exporter), per-namespace container CPU/memory (via cAdvisor).
  - **PostgreSQL Ops & Queries**: connections, transaction/tuple rates, buffer cache hit ratio,
    locks, checkpoints, and per-query call rate/latency - see
    [PostgreSQL Metrics (postgres_exporter)](#postgresql-metrics-postgres_exporter) below.
  - **Hazelcast Cache**: cluster size, connected clients, cache hit ratio/operation rate/latency
    for the `messages` map, member heap, GC time - see
    [Hazelcast Metrics (JMX exporter)](#hazelcast-metrics-jmx-exporter) below.
  - **OpenObserve Ops**: self-monitoring for the OpenObserve backend itself (up/down, disk usage,
    process RSS/CPU/open fds/uptime, HTTP request rate & p95/p99 latency by endpoint, ingest
    rate/bytes by stream type, in-memory MemTable size, WAL bytes) - scraped from OpenObserve's own
    `/metrics`, which is off by default (`config.ZO_PROMETHEUS_ENABLED` in
    `k8s/observability/openobserve-values.yaml` - confirmed live: with it off, `/metrics` returns
    HTTP 200 with an empty body). The MemTable Size panel is worth watching directly - that's the
    exact thing that overflowed (see the "kind cluster ops" scope note above) before OpenObserve's
    resources were bumped and remote_write was scoped down.
- **Prometheus** (not exposed via Ingress; use `kubectl port-forward -n observability svc/prometheus 9090:9090`
  if you want its own UI at `http://localhost:9090`): scrapes `otel-collector.observability.svc.cluster.local:8889`
  (app metrics), `postgres.default.svc.cluster.local:9187` (postgres_exporter) and
  `hazelcast.default.svc.cluster.local:9404` (Hazelcast's JMX exporter) cross-namespace - see below
  for both - plus `kube-state-metrics` and every `node-exporter` pod, and every node's kubelet
  cAdvisor endpoint via the API server proxy (see `k8s/observability/config/prometheus.yml` and
  `prometheus-rbac.yaml`) for the cluster-ops dashboard.
- **OpenObserve**: `http://openobserve.localhost/` — log in with credentials configured via Secrets / 1Password
  (see `k8s/observability/openobserve-values.yaml`). It receives the
  message-service's own metrics plus kind cluster ops metrics (Prometheus `remote_write`s the
  `otel-collector`, `node-exporter`, `kube-state-metrics`, and `kubernetes-nodes-cadvisor` jobs into it
  — see `write_relabel_configs` in `k8s/observability/config/prometheus.yml`; every other scraped
  job is deliberately dropped before it reaches OpenObserve), under org `default`, one stream per
  Prometheus metric name (e.g. `http_requests_total`,
  `container_memory_working_set_bytes`, `node_memory_MemAvailable_bytes`). Query it from the UI's
  Logs/Metrics explorer, or via its search API:
  ```bash
  NOW_US=$(( $(date +%s) * 1000000 )); START_US=$(( NOW_US - 3600*1000000 ))
  curl -s -u "$ZO_ROOT_USER_EMAIL:$ZO_ROOT_USER_PASSWORD" -X POST 'http://openobserve.localhost/api/default/_search?type=metrics' \
    -H 'Content-Type: application/json' \
    -d "{\"query\":{\"sql\":\"SELECT * FROM \\\"http_requests_total\\\" ORDER BY _timestamp DESC LIMIT 5\",\"start_time\":$START_US,\"end_time\":$NOW_US,\"size\":5}}"
  ```
  (`start_time`/`end_time` are epoch microseconds; OpenObserve rejects a query whose range doesn't
  look like one, e.g. `0`.)
- **Headlamp**: `http://headlamp.localhost/` — a general-purpose Kubernetes dashboard (not a
  metrics tool), for browsing/inspecting/editing any resource across the whole cluster: pods,
  deployments, logs, exec-into-pod, node status, etc. Log in with a bearer token:

  1. Generate a token for the chart's own `headlamp` ServiceAccount, which already has
     `cluster-admin` via its default `ClusterRoleBinding` (see `k8s/headlamp/headlamp-values.yaml`):
     ```bash
     kubectl create token headlamp -n headlamp --duration=24h
     ```
  2. Open `http://headlamp.localhost/` and paste the token into the login page's token field.
     On macOS, pipe straight to the clipboard instead of copying from a terminal selection - long
     tokens are prone to picking up a stray line break or trailing whitespace when copied by hand,
     which Headlamp will reject as an invalid token:
     ```bash
     kubectl create token headlamp -n headlamp --duration=24h | tr -d '\n' | pbcopy
     ```
  3. The token expires after `--duration` (24h above) - re-run the command and log in again once
     it does; there's no refresh flow.
- **OTel Collector** (`k8s/observability/config/otel-collector.yaml`): receives OTLP metrics on
  `:4317` (gRPC) / `:4318` (HTTP) from every `message-service` pod
  (`OTEL_METRICS_URL` in `k8s/configmap.yaml` points at it) and re-exports them in Prometheus format
  on `:8889`. Two things had to be true for per-pod metrics to actually work, both already wired
  up: (1) `resource_to_telemetry_conversion.enabled: true` on the Prometheus exporter - without it,
  OTLP *resource* attributes like `k8s.pod.name` are dropped rather than becoming Prometheus labels,
  so metrics from every pod collapse into one indistinguishable series; (2) the app itself has to send
  that resource attribute in the first place (`app/telemetry.py`, sourced from a `POD_NAME` Downward
  API env var in `k8s/deployment.yaml`).

Prometheus here has no `PersistentVolumeClaim` — its data is ephemeral and resets whenever its pod
restarts. That's fine for a local metrics-exploration setup; add a PVC to `prometheus-deployment.yaml`
(or switch to a `StatefulSet` like `postgres`) if you want it to survive restarts.

### PostgreSQL Metrics (postgres_exporter)

[`postgres_exporter`](https://github.com/prometheus-community/postgres_exporter) runs as a sidecar
in the `postgres-0` pod (see `k8s/postgres-statefulset.yaml`) - it shares the pod's network
namespace, so it reaches Postgres over `localhost:5432` using the same `postgres-credentials`
Secret the app already uses. `k8s/postgres-service.yaml` exposes it on a `metrics` port (`9187`)
alongside Postgres' own `5432`, and Prometheus (running in the separate `observability` namespace)
scrapes it cross-namespace via `postgres.default.svc.cluster.local:9187`.

**Ops metrics** come from postgres_exporter's built-in collectors, enabled by default: connection
counts and per-state breakdown (`pg_stat_activity_count`), transaction/tuple rates and cache hit
ratio (`pg_stat_database_*`), lock counts by mode (`pg_locks_count`), and checkpoint/buffer activity
(`pg_stat_bgwriter_*`) - note the `stat_` infix; there is no bare `pg_bgwriter_*` metric.

**Query metrics** need the `pg_stat_statements` extension, which isn't in Postgres by default:

1. The `postgres` container's startup args add `shared_preload_libraries=pg_stat_statements` (must
   happen at server start, not via SQL) and `pg_stat_statements.track=all` (also count statements
   run inside functions).
2. `CREATE EXTENSION IF NOT EXISTS pg_stat_statements` is issued by the first Alembic revision
   (`migrations/versions/0001_initial_schema.py`) - it attaches to that already-preloaded library on
   first deploy.

3. The exporter's `--collector.stat_statements` flag (plus `--collector.stat_statements.include_query`
   for a `queryid` -> SQL-text mapping) exposes per-`queryid` call count, total time, and rows via
   `pg_stat_statements_calls_total` / `_seconds_total` / `_rows_total` - the numeric metrics are
   labeled by `queryid`/`user`/`datname` only (not the SQL text itself, to keep cardinality sane);
   `pg_stat_statements_query_id` is the separate `queryid` -> `query` lookup table, capped at the
   top 20 statements and 1024 characters each by the exporter's own defaults.

If you change the postgres container's startup args or the `postgres-exporter` sidecar, the
StatefulSet needs `kubectl apply -k k8s/` (it's part of the main Kustomization, not
`k8s/observability/`); an Alembic migration change needs the app image rebuilt and redeployed (see
"Pushing a code change to the running cluster" above) since `alembic upgrade head` runs from the
image's own `migrations/` directory. Prometheus config changes need a `kubectl rollout
restart deployment/prometheus -n observability` too - it has no `--web.enable-lifecycle` reload
endpoint wired up, so it only reads `prometheus.yml` at startup.


### Hazelcast Metrics (JMX exporter)

Hazelcast's own [Management Center has a built-in Prometheus exporter](https://docs.hazelcast.com/management-center/5.11/integrate/prometheus-monitoring)
(`hazelcast.mc.prometheusExporter.enabled`), but it turned out to be an **Enterprise-licensed
feature** - confirmed live by deploying Management Center and getting `402 LICENSE_REQUIRED` from
its `/metrics` endpoint, not just by reading the docs. Rather than requiring a paid license for a
local dev cluster, `k8s/hazelcast-deployment.yaml` instead attaches
[`jmx_prometheus_javaagent`](https://github.com/prometheus/jmx_exporter) directly to the Hazelcast
member's own JVM - a free, open-source, in-process javaagent (no separate Hazelcast license, no
remote JMX/RMI port needed) that reads the member's JMX MBeans and re-exposes them as Prometheus
text format on its own port. This is entirely on the Hazelcast server side and unaffected by the
app's own language - the standalone `hazelcast/hazelcast:5.7.0-slim` image still runs its own JVM
regardless of what language the client (`message-service`) is written in.

- An `initContainer` (`curlimages/curl`) downloads the agent jar into a volume shared with the
  `hazelcast` container on every pod start, rather than baking it into a custom Hazelcast image.
- The `hazelcast` container's `JAVA_OPTS` sets `-Dhazelcast.metrics.jmx.enabled=true` (registers
  Hazelcast's cluster/map/operation stats as `com.hazelcast:*` JMX MBeans - off by default) and
  `-javaagent:...=9404:/etc/jmx-exporter/config.yaml` (port `9404` is the agent's own listener,
  unrelated to Hazelcast's member port `5701`). The agent's config
  (`k8s/hazelcast-jmx-exporter-configmap.yaml`) just whitelists `com.hazelcast:*` and uses the
  exporter's default attribute-derived naming rather than hand-written per-metric rules.
- `k8s/hazelcast-service.yaml` exposes port `9404` alongside `5701`, and Prometheus scrapes it
  cross-namespace via `hazelcast.default.svc.cluster.local:9404`.

**Verified metric names** (checked live via port-forward before writing dashboard panels - the
exporter's default naming isn't documented anywhere, and Hazelcast's own MBean layout isn't
guaranteed stable across versions): `com_hazelcast_metrics_<attribute>`, labeled by `prefix` (the
metric's category - `cluster`, `map`, `memory`, `gc`, `tcp.connection`, `client.endpoint`, etc.) and
`tag0` for per-instance metrics (e.g. a map name). Per-map metrics come back with `tag0` set to the
*literal string* `"name=messages"` **including the embedded quote characters** (an artifact of how
Hazelcast quotes JMX ObjectName tags, which the exporter just passes through) - matching that
exactly requires escaping those quotes twice over (once for PromQL, once for JSON), so every panel
here uses a `tag0=~".*name=messages.*"` regex substring match instead, which sidesteps the
escaping entirely. Key metrics used: `com_hazelcast_metrics_size{prefix="cluster"}` (member count),
`com_hazelcast_metrics_count{prefix="client.endpoint"}` (connected clients - matches
`message-service`'s pod count, since each pod is a Hazelcast client), `hits`/`getcount`/`putcount`/
`removecount`/`totalgetlatency`/`totalputlatency`/`ownedentrycount`/`ownedentrymemorycost`/
`evictioncount`/`expirationcount` (all `prefix="map"`, per-map via `tag0`), and
`usedheap`/`committedheap`/`maxheap` (`prefix="memory"`).

### HTTP Operation & Error Dashboards

`http-operations.json` and `http-errors.json` break `http_requests_total`,
`http_request_duration_ms` and `http_errors_total` (all recorded by the request-metrics middleware in
`app/telemetry.py`) down by what the **API** says, not by what the client happened to send:

| Label | Values | Notes |
| :--- | :--- | :--- |
| `method` | `GET`, `POST`, `PATCH`, `DELETE` | |
| `route` | the route *template*: `/messages`, `/messages/{id}`, `/authors`, `/authors/{id}`, `unmatched` | Taken from the matched route, never the raw path, so cardinality is bounded by the API's routes. `unmatched` = no route matched (404/405). |
| `status_code` (requests only) | the HTTP status | |
| `error_code` (errors only) | `BAD_USER_INPUT`, `NOT_FOUND`, `CONFLICT`, `INTERNAL_SERVER_ERROR`, `HTTP_405`, ... | The problem+json `code` of the response - a small closed set. |

- **Why the route template and not the raw path?** `/messages/<uuid>` would mint a new series per
  message: the client would control cardinality (the collector and OpenObserve both pay for that -
  OpenObserve has already overflowed its MemTable once).
- **Health probes are excluded.** `/health/liveness` and `/health/readiness` are hit by the kubelet
  every few seconds and would swamp real traffic; the middleware skips them.
- **`http_errors_total` counts every response with status >= 400**, client errors included
  (`NOT_FOUND`, `BAD_USER_INPUT`, `CONFLICT`). `INTERNAL_SERVER_ERROR` is the one that means a bug or
  a failed dependency; the *Server Errors/s* panel on the HTTP Errors dashboard should stay at 0.
- `http_errors_total` is only created on the first error (an OTel counter emits nothing until its
  first `.add()`), so panels built directly on it are empty - not zero - on a healthy system.
  The ratio and total panels use `or vector(0)`; the by-code panels show "No errors".

### API RED & Saturation Dashboard

`api-red.json`'s panels follow the standard [RED method](https://grafana.com/blog/2018/08/02/the-red-method-how-to-instrument-your-services/)
(**R**ate, **E**rrors, **D**uration) plus enough saturation signal to explain *why* rate/errors/
duration are moving, for the API picked in the **Service** dropdown and its direct dependencies:

- **Rate**: `http_requests_total` overall and broken down by route (see
  [HTTP Operation & Error Dashboards](#http-operation--error-dashboards) and the middleware in
  `app/telemetry.py`).
- **Duration**: p50/p90/p95/p99 latency (overall and per route) from `http_request_duration_ms`.
- **Errors**: overall error ratio (gauge, thresholds at 1%/5%) and error rate by route, both from
  `http_errors_total` - a request counts as an error whenever it answers HTTP >= 400; the breakdown
  by problem+json `code` lives on the HTTP Errors dashboard.
- **Saturation**: DB connection-pool utilization (busy/idle/open) and average query duration
  (`db_pool_connections_*` / `db_client_queries_duration_avg_ms`, read from the SQLAlchemy pool and
  statement-timing events - see `setup_metrics` in `app/telemetry.py`), CPU throttling ratio,
  container memory vs. its limit, and pod restarts/readiness.

No `uri!~"/health.*"`-style filter is needed: the request-metrics middleware skips `/health/*`
before recording anything, so there's no probe traffic to exclude in the first place.

**Deliberately not implemented, so not claimed as covered by this dashboard**: 429/timeout/retry
rates (the app has no rate limiting or explicit downstream timeouts to measure), business-outcome
errors beyond an unhandled exception (out of scope per this dashboard's own design goal), deployment
markers, and trace exemplars. Downstream dependency RED for Postgres and Hazelcast already exist as
their own dashboards (linked above) rather than being duplicated here.

---

## Load Testing with k6

The repository includes five parameterized [k6](https://k6.io/) scripts using `k6-utils` to benchmark
and simulate concurrent REST traffic against the API. They all follow the same conventions (same
environment variables, same VU/think-time shape), so any of the "Running the Load Tests" commands
below work with any of them - just swap the filename.

| Script | What it exercises |
| :--- | :--- |
| `k6-retrieve-messages.js` | `GET /messages` (read path), including a paginated `?limit=5` request - see [Pagination](#pagination). |
| `k6-create-messages.js` | `POST /messages` (write path); creates one shared author in `setup()`. |
| `k6-message-lifecycle.js` | Full CRUD per iteration: `POST /messages` -> `GET /messages/{id}` -> `PATCH /messages/{id}` -> `DELETE /messages/{id}`. |
| `k6-invalid-requests.js` | Negative paths: invalid create (400 `BAD_USER_INPUT` + `invalidParams`), unknown id (404 `NOT_FOUND`), malformed id (400), malformed JSON (400) - see [REST API](#rest-api). |
| `k6-transaction-isolation.js` | Concurrency/lost-update regression test for `PATCH /messages/{id}`'s optimistic locking - see [Concurrency & Transaction Isolation](#concurrency--transaction-isolation). |

Metrics and the Grafana dashboards break traffic down by route template (`POST /messages`,
`GET /messages/{id}`, ...), and each script tags its requests with a k6 `name` (`CreateMessage`,
`GetMessageById`, `IncrementCounter`, ...) so k6's own per-request output stays readable.

### Prerequisites

Install `k6` using Homebrew or your package manager:

```bash
brew install k6
```

### Script Configuration

Every script supports the same environment variables:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `VUS` | Number of concurrent virtual users | `10` |
| `DURATION` | Duration of the test run (e.g. `10s`, `1m`) | `10s` |
| `BASE_URL` | Target base URL (no trailing slash) | `http://localhost` |

**Built-in Thresholds:**
- `http_req_failed`: error rate must remain below 1% (`rate<0.01`).
- `http_req_duration`: 95th percentile latency must be under 500ms (`p(95)<500`).

`k6-invalid-requests.js` is the one exception: every request in it *intentionally* triggers a 4xx, so
`http_req_failed` isn't a meaningful signal there. It marks 400/404 as expected per request and uses
`checks: ['rate>0.99']` instead, which measures what actually matters for that script: did the API
return the *correct* status and problem+json shape essentially every time.

`k6-transaction-isolation.js` also deviates: a `409` from a losing optimistic-lock race is an
*expected*, correct response, not a failure (`http.setResponseCallback(http.expectedStatuses(...))`),
and it adds a `no_lost_updates` threshold - see the next section for what it's actually checking.

### Running the Load Tests

#### 1. Default Run (10 VUs for 10s)
```bash
k6 run k6-retrieve-messages.js
```

#### 2. Parameterized using `-e` flags (Recommended)
```bash
k6 run -e VUS=25 -e DURATION=30s k6-create-messages.js
```

#### 3. Parameterized using shell environment variables
```bash
VUS=50 DURATION=1m k6 run k6-message-lifecycle.js
```

#### 4. Custom endpoint / remote target
```bash
k6 run -e BASE_URL=http://localhost:8080 -e VUS=20 -e DURATION=15s k6-invalid-requests.js
```

## Concurrency & Transaction Isolation

**Isolation-level review.** Nothing in this codebase sets a transaction isolation level anywhere -
SQLAlchemy runs each request in one transaction against PostgreSQL's unmodified default
(`READ COMMITTED`). Tuning that level wouldn't have mattered here, though: the real risk isn't
*which* isolation level applies to each statement, it's that a naive update could run its read and
its write as two entirely separate, uncoordinated operations - no isolation level closes a gap
between two unrelated round trips.

**The bug this guards against.** A classic lost update: two concurrent callers could both read the
same row, then both write, with the second write silently overwriting the first's change with no
error to either caller.

**The fix - optimistic locking via a `version` column.** `Message` has a `version` integer
(default `0`, see `app/models.py`). Every response carries it, and `PATCH /messages/{id}` requires
the caller to send back the version it read:

```bash
curl -X PATCH http://localhost/messages/<id> -H 'Content-Type: application/json' \
  -d '{"content": "New content", "version": 3}'
```

`update_message` (see `app/services/messages.py`) applies the write conditionally, in a single
statement:

```sql
UPDATE messages SET content = :content, version = version + 1
WHERE id = :id AND version = :version RETURNING id
```

using **the version the client submitted**, not a version the server re-reads for itself. That
distinction matters: guarding against a server's own just-read value only protects the few
milliseconds between that read and its own write; it can't tell whether the *client's* value was
stale, and a stale client value is exactly what happens on a real read-then-update flow. If the row
has moved on since the client's read, no row matches; the service then checks whether the id exists
(`404` if not) and otherwise answers `409 CONFLICT` telling the caller to refetch and retry - instead
of silently losing their change.

**Cache correctness under contention.** Reads are cache-aside, and the cache has no TTL. A slow
reader can load the pre-update row from Postgres and write it into the cache *after* the update's
eviction, leaving a stale entry that nothing would ever remove - and since clients then keep
reading the stale version, every one of their updates would `409` forever. So a `409` also evicts the
entry: the client's version was stale, so the cached copy may be too, and the next read reloads from
the database.

**Verifying it - `k6-transaction-isolation.js`.** The script has many VUs race to increment a
counter kept in one message's `content` field: each iteration reads (`content` and `version`) then
`PATCH`es `content + 1` guarded by the `version` it just read. Every success must correspond to a real,
distinct `+1`; `409`s are expected under contention and are reported separately
(`write_conflicts`), not counted as failures. In teardown it checks the invariant that ties the two
together: starting from `content = "0"` / `version = 0`, each successful write adds exactly 1 to
both, so they stay equal **if and only if** no write was applied against a stale read. That is the
`no_lost_updates` threshold, so a lost update fails the run. Run it and compare the custom metrics:

```bash
VUS=20 DURATION=15s k6 run k6-transaction-isolation.js
```

```
Final state: counter (content) = 420, version = 420.
no_lost_updates................: 100.00% 1 out of 1
successful_increments..........: 420     ...
write_conflicts................: 4195    ...
```

`successful_increments`, the final counter and the final version should all be equal. To see the
authoritative value, query Postgres directly:

```bash
kubectl exec -i postgres-0 -- psql -U message_app -d messagedb \
  -c "SELECT content, version FROM messages WHERE title = 'k6-transaction-isolation counter';"
```

(The script deletes its counter message in teardown, so query it while the run is in progress if you
want to see it there.)

**Note:** because `PATCH /messages/{id}` requires `version`, `k6-message-lifecycle.js`'s update step
sends `version: 0` (correct immediately after its own `POST` step, since a freshly created message
always starts at version 0).

## Pagination

`GET /messages` and `GET /authors` return a page `{ "items": [...], "totalCount": n }` rather than the
whole table in one response - unbounded against a table that's had any real traffic. They accept two
optional query parameters:

| Parameter | Description | Default | Bounds |
| :--- | :--- | :--- | :--- |
| `limit` | Max number of items to return | `50` | `1`-`200` |
| `offset` | Number of items to skip | `0` | `>= 0` |

```bash
curl 'http://localhost/messages?limit=20&offset=40'
```

`totalCount` carries the *total* row count, independent of `limit`/`offset`, so a client can compute
how many pages remain (`ceil(totalCount / limit)`). Out-of-range values (`limit=0`, `limit=500`,
`offset=-1`, ...) are rejected with `400 BAD_USER_INPUT` rather than silently clamped, through the
same validation path (and the same problem+json body) as every other request.

Messages are ordered `created_at DESC, id DESC` (newest first); `id` is the tiebreaker so paging
stays stable even when two rows share the same `created_at` - without it, ties could reorder across
pages and either skip or repeat a row. A matching index, `ix_messages_created_at_id` on
`(created_at, id)` (see `app/models.py`), keeps that sort itself from scanning the whole table on every
request; dropping it turns pagination into a full-table sort per page, which on a large table is the
difference between double-digit-millisecond and multi-second responses.

## REST API

The full surface - endpoints, status codes, validation rules, the error model - is in
[API-DESIGN.md](API-DESIGN.md). In short:

| Method and path | Success | Errors |
| :--- | :--- | :--- |
| `GET /messages?limit&offset` | 200 page | 400 |
| `GET /messages/{id}` | 200 | 400 (bad UUID), 404 |
| `POST /messages` | 201 + `Location` | 400, 404 (unknown author) |
| `PATCH /messages/{id}` | 200 (`version` + 1) | 400, 404, **409** (stale version) |
| `DELETE /messages/{id}` | 204 | 400, 404 |
| `GET /authors?limit&offset` | 200 page | 400 |
| `GET /authors/{id}[?include=messages]` | 200 | 400, 404 |
| `POST /authors` | 201 + `Location` | 400, 409 (duplicate email) |
| `PATCH /authors/{id}` | 200 | 400, 404, 409 |
| `DELETE /authors/{id}` | 204 | 400, 404, **409** (author still has messages) |

**Errors are RFC 9457 `application/problem+json`**, with a stable machine-readable `code`:

```json
{
  "type": "/problems/bad-user-input",
  "title": "Bad Request",
  "status": 400,
  "detail": "The request content was invalid or failed validation constraints.",
  "instance": "/messages",
  "code": "BAD_USER_INPUT",
  "invalidParams": [{ "name": "title", "reason": "title is required and cannot be blank" }]
}
```

Validation failures are `400` (FastAPI's default `422` is overridden) and list **every** failing
field at once. An unhandled exception is a generic `500 INTERNAL_SERVER_ERROR` with no stack trace
(the details are logged with the trace id). See [`k6-invalid-requests.js`](k6-invalid-requests.js) for
the client-error cases exercised directly.

**API docs.** FastAPI generates the OpenAPI 3.1 document from the code, so there's no spec file to keep
in sync by hand. `/docs` (Swagger UI), `/redoc` and `/openapi.json` are served only when
`API_DOCS_ENABLED` is exactly `"true"` (any other value but `"false"` fails startup); the dev
cluster's `k8s/configmap.yaml` turns it on. Leave it off anywhere real.

**CORS.** Browsers on other origins are blocked by default; list the ones that may call the API in
`CORS_ALLOWED_ORIGINS` (comma-separated exact origins). curl, k6 and other non-browser clients aren't
affected by CORS.

**Request size.** Bodies over 16 KiB are rejected with `413`; together with the pagination bounds and
the per-field `max_length` checks (title 100, content 1000, name 50, email 100) that bounds the cost
of any single request.
