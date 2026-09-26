# ArgoCD — commands & troubleshooting

Day-to-day ArgoCD commands for this repo's local kind cluster. Everything is `kubectl`-based: the
`argocd` CLI isn't installed here (an optional CLI section is at the end). For the design — what
ArgoCD owns and why — see the "Continuous Deployment with ArgoCD" section of `README.md`; for
`argocd-repo-server` liveness-probe failures see `KUBECTL.md`.

Commands marked ✅ were run against this cluster while writing this; the rest are standard ArgoCD /
kubectl usage that I did not run here.

## What's in the cluster

| Thing | Name | Defined in |
| :--- | :--- | :--- |
| Application: `default` namespace (Postgres, Hazelcast, message-service, Ingress, quotas) | `fastapi-o2` | `k8s/argocd/application.yaml`, syncs `k8s/` |
| Application: Kyverno policies + PolicyExceptions | `kyverno-policies` | `k8s/argocd/policies-application.yaml`, syncs `k8s/policies/` |
| Application: observability stack (OTel Collector, Prometheus, Grafana + dashboards, kube-state-metrics, node-exporter, log collector, the namespace) | `observability` | `k8s/argocd/observability-application.yaml`, syncs `k8s/observability/` |
| Application: Trivy Operator (upstream Helm chart + values from this repo, into `trivy-system`) | `trivy-operator` | `k8s/argocd/trivy-operator-application.yaml`, chart `aqua/trivy-operator` + `$values/k8s/trivy-operator/` - see [TRIVY.md](TRIVY.md) |
| Image Updater config | `ImageUpdater/fastapi-o2` | `k8s/argocd/image-updater.yaml` |
| UI | `http://argocd.localhost/` | `k8s/argocd/ingress.yaml` |

All three Applications track `main` with `automated: {prune: true, selfHeal: true}`. **Not** managed by
ArgoCD: OpenObserve, Headlamp and Kyverno themselves (all Helm installs done by `deploy-kind.sh`;
only the Kyverno *policies* are an Application).

ArgoCD components in the `argocd` namespace: `argocd-server`, `argocd-repo-server`,
`argocd-redis`, `argocd-dex-server`, `argocd-applicationset-controller`,
`argocd-notifications-controller`, `argocd-image-updater-controller` (Deployments) and
`argocd-application-controller` (StatefulSet).

## Status

```bash
kubectl get applications -n argocd                                                       # ✅ sync + health at a glance

# one Application: sync status, health, deployed commit, last operation
kubectl get application fastapi-o2 -n argocd \
  -o jsonpath='{.status.sync.status} {.status.health.status} rev={.status.sync.revision} {.status.operationState.phase}{"\n"}'   # ✅

# per-resource status (spot the OutOfSync / Degraded one)
kubectl get application fastapi-o2 -n argocd \
  -o jsonpath='{range .status.resources[*]}{.kind}/{.name} {.status} {.health.status}{"\n"}{end}'                              # ✅

# errors (ComparisonError, SyncError, ...) - empty output means none
kubectl get application fastapi-o2 -n argocd -o jsonpath='{.status.conditions}{"\n"}'                          # ✅

# last deployments: id, commit, time
kubectl get application fastapi-o2 -n argocd \
  -o jsonpath='{range .status.history[-3:]}{.id} {.revision} {.deployedAt}{"\n"}{end}'                                        # ✅

kubectl describe application fastapi-o2 -n argocd                          # everything above plus events
```

Compare the deployed commit with `main`: `git rev-parse origin/main` vs `.status.sync.revision`. A
merge is only live once these match — ArgoCD polls git roughly every 3 minutes, so right after a
merge it is normal for the Application to still show the previous commit.

**Health `Progressing`** right after a merge is usually just a rolling update in flight
(`kubectl rollout status deploy/message-service`); it goes back to `Healthy` on its own.

## Forcing a refresh or a sync (without the CLI)

Both of these **modify the live Application object**, so they are worth doing on purpose rather than
by reflex; waiting for the next poll is always the no-touch option.

```bash
# Re-poll git now (ArgoCD removes the annotation itself once handled). "hard" also drops the
# manifest cache - use it if a refresh keeps returning stale state.
kubectl annotate application kyverno-policies -n argocd argocd.argoproj.io/refresh=normal --overwrite
kubectl annotate application kyverno-policies -n argocd argocd.argoproj.io/refresh=hard --overwrite

# Trigger a sync operation (what the UI's "Sync" button does)
kubectl patch application fastapi-o2 -n argocd --type merge \
  -p '{"operation":{"sync":{"prune":true}}}'
```

With `automated` sync on, a refresh alone is normally enough: once ArgoCD sees the new commit it
syncs by itself.

## Image Updater

CI pushes `docker.io/miqui/<service>:<yyyymmddHHMMSS>-<sha7>`; Image Updater picks the newest tag
(`alphabetical` strategy) roughly every 2 minutes and writes it into the Application — **no git
commit is made**.

```bash
# the tag each service is currently pinned to (the override lives on the Application, not in git)
kubectl get application fastapi-o2 -n argocd \
  -o jsonpath='{.spec.source.kustomize.images}{"\n"}'                                     # ✅

kubectl get imageupdater -n argocd                                                        # ✅ READY, LAST CHECKED
kubectl logs -n argocd deploy/argocd-image-updater-controller --tail=20                   # ✅
kubectl logs -n argocd deploy/argocd-image-updater-controller | grep 'images_updated'     # ✅ per-cycle summary
```

A healthy idle cycle logs `images_considered=1 images_skipped=0 images_updated=0 errors=0`. To
confirm a rollout picked up a build, compare the tag above with the pods':

```bash
kubectl get pods -n default -l app=message-service \
  -o jsonpath='{range .items[*]}{.metadata.name} {.spec.containers[0].image}{"\n"}{end}'  # ✅
```

Image tags end in the short SHA of the commit that was built, so `…-3755170` is the build of merge
commit `3755170`.

## Pausing auto-sync (local testing only)

`selfHeal` reverts hand edits to anything ArgoCD owns within minutes, and Image Updater keeps
moving the image tags. To test a locally built image or a hand-patched ConfigMap you must pause
auto-sync first — and remember to restore it.

```bash
# pause: remove the automated block (manual sync still works)
kubectl patch application fastapi-o2 -n argocd --type json \
  -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'

# restore
kubectl patch application fastapi-o2 -n argocd --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
```

While paused, `kubectl get application …` will show `OutOfSync` for anything you changed by hand.

## Drift you should expect

`postgres-credentials` is a committed placeholder (`k8s/secret.yaml`) that `deploy-kind.sh`
overwrites with real values. `application.yaml` tells ArgoCD to ignore that Secret's data
(`ignoreDifferences` + `RespectIgnoreDifferences=true`), so it never shows as `OutOfSync` and a sync
never puts the placeholder back.

`grafana-credentials` and `openobserve-remote-write-credentials` (the `observability` Application) are the
same story: committed placeholders that `deploy-kind.sh` overwrites, ignored via `ignoreDifferences`
+ `RespectIgnoreDifferences=true` in `observability-application.yaml`.

**Hash-suffixed ConfigMaps.** `prometheus-config-<hash>`, `otel-collector-config-<hash>` and
`log-collector-config-<hash>` are generated by kustomize (`configMapGenerator` in
`k8s/observability/kustomization.yaml`, sources in `k8s/observability/config/`). Editing one of those files
changes the hash, which changes the pod template and rolls the workload - those three components read
their config once at startup and have no reload, so a plain ConfigMap update would leave the old config
running. After a sync Argo prunes the previous hash-named ConfigMap. Prometheus keeps its TSDB in the
container filesystem (no volume), so each such roll drops its local history; the series that are
`remote_write`n to OpenObserve are unaffected. The Grafana ConfigMaps are plain manifests - Grafana re-scans
its dashboard directory every 30s, so no restart is needed.

**Hand edits get reverted.** With `selfHeal` on, `kubectl apply`-ing an observability manifest from an
unmerged branch is undone on the next reconcile. Change it in git and merge instead.

**The `observability` namespace is never pruned** (`argocd.argoproj.io/sync-options: Prune=false` in
`k8s/observability/namespace.yaml`): deleting the namespace would take OpenObserve's PVC with it.

**One-time bootstrap on an existing cluster** (a fresh `deploy-kind.sh` does this itself): the
Application can't create itself, so after the PR that adds it is merged:

```bash
kubectl apply -f k8s/argocd/observability-application.yaml
kubectl get application observability -n argocd   # expect Synced; Argo adopts the existing objects
```

Adoption changes only the Namespace annotation, swaps the three ConfigMaps for their hash-named copies,
and rolls Prometheus, the OTel Collector and the log collector once (their volume reference changed).

Argo only prunes objects it has tracked, and the pre-existing un-suffixed `prometheus-config`,
`otel-collector-config` and `log-collector-config` were applied by kubectl, so they are left behind as
unreferenced orphans. Once the three workloads have rolled, remove them by hand (a fresh cluster never
has them):

```bash
kubectl delete configmap prometheus-config otel-collector-config log-collector-config -n observability
```

## Rolling back

With `automated` sync enabled ArgoCD refuses `argocd app rollback`. The GitOps way is to
`git revert` the bad merge and let ArgoCD sync `main`. For an emergency image rollback, pause
auto-sync (above) and set the previous tag from `.status.history`, or revert and merge — Image
Updater will otherwise move the tag forward again.

## Troubleshooting

**`ComparisonError` / sync status `Unknown`.** ArgoCD can't render the manifests, so it can't say
whether anything is in sync. Seen live on `kyverno-policies` (2026-09-21):

```
Failed to load target state: failed to generate manifest for source 1 of 1: rpc error:
code = Unavailable desc = dns: A record lookup error: lookup argocd-repo-server on 10.96.0.10:53:
dial udp 10.96.0.10:53: i/o timeout
```

That is the application-controller failing to reach `argocd-repo-server` — the repo-server had just
been restarted by its liveness probe (5 restarts at the time). It is transient: it clears on the
next reconcile once the repo-server is back, and the fix for the restarts themselves is in
`KUBECTL.md` ("Diagnosing `argocd-repo-server` liveness probe failures").

```bash
kubectl get pod -n argocd -l app.kubernetes.io/name=argocd-repo-server        # ✅ RESTARTS column
kubectl logs -n argocd deploy/argocd-repo-server --since=10m | grep '"level":"error"'   # ✅
kubectl logs -n argocd statefulset/argocd-application-controller --since=10m            # sync/compare decisions
kubectl get events -n argocd --sort-by=.lastTimestamp
```

**Merged but nothing changed.** In order: is `.status.sync.revision` the merge commit yet (poll
delay, or a `ComparisonError` above)? Is the change under `k8s/` at all — `k8s/observability/` is
**not** synced and must be applied by hand? For a new image: did CI push it, and does the
Image Updater log show `images_updated=1`?

**Stuck `Progressing` / `Degraded`.** Find the resource with the per-resource query above, then
debug that resource (`kubectl describe`, `kubectl get events`). A Deployment rejected by Kyverno in
`default` shows up here as a sync error naming the policy.

**Sync fails on a policy.** The enforce policies deny non-compliant workloads in `default` when
ArgoCD applies them — see the "Working with the policies" section of `README.md` and `KYVERNO.md`.

## Login

```bash
# UI: http://argocd.localhost/  - user admin, password:
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```

(`argocd-initial-admin-secret` exists on this cluster ✅; ArgoCD's docs recommend deleting it after
you change the password.)

## Optional: the `argocd` CLI

Not installed here, and none of the commands below were run. `argocd-server` is patched with
`--insecure` and reached through the plain-HTTP `*.localhost` Ingress, hence the flags:

```bash
argocd login argocd.localhost --plaintext --grpc-web --username admin

argocd app list
argocd app get fastapi-o2
argocd app diff fastapi-o2          # live vs git
argocd app sync fastapi-o2
argocd app history fastapi-o2
argocd app set fastapi-o2 --sync-policy none      # pause auto-sync
```
