# kubectl Commands — OpenObserve Observability Rollout

`kubectl` commands used while adding OpenObserve observability for the message REST API,
grouped by phase.

## From `deploy-kind.sh` (the automated deployment)

```bash
kubectl config use-context "kind-${CLUSTER_NAME}"
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.3/deploy/static/provider/kind/deploy.yaml
kubectl wait --namespace ingress-nginx --for=condition=ready pod --selector=app.kubernetes.io/component=controller --timeout=120s
kubectl apply -k k8s/observability/
kubectl apply -k k8s/
kubectl rollout status statefulset/postgres --timeout=120s
kubectl rollout status deployment/hazelcast --timeout=120s
kubectl rollout status deployment/otel-collector -n observability --timeout=120s
kubectl rollout status deployment/prometheus -n observability --timeout=120s
kubectl rollout status deployment/grafana -n observability --timeout=120s
kubectl rollout status statefulset/openobserve -n observability --timeout=180s
kubectl rollout status deployment/message-service --timeout=180s
kubectl get nodes -L workload -o wide
kubectl get pods -l app=postgres -o wide
kubectl get pods -l app=hazelcast -o wide
kubectl get pods -l app=message-service -o wide
kubectl get svc message-service
kubectl get pods -n observability -o wide
```

## Manual investigation (diagnosing why ingress-nginx wasn't ready)

```bash
kubectl config use-context kind-kind-fastapi-cluster
kubectl get nodes -o wide
kubectl get pods -n ingress-nginx -o wide
kubectl describe pod -n ingress-nginx -l app.kubernetes.io/component=controller
```

## Manual verification (Prometheus remote_write → OpenObserve)

```bash
kubectl exec -n observability deploy/prometheus -- wget -qO- 'http://localhost:9090/api/v1/query?query=prometheus_remote_storage_succeeded_samples_total'
kubectl exec -n observability deploy/prometheus -- wget -qO- 'http://localhost:9090/api/v1/query?query=prometheus_remote_storage_samples_failed_total'
kubectl exec -n observability deploy/prometheus -- wget -qO- 'http://localhost:9090/api/v1/query?query=up{job="otel-collector"}'
kubectl exec -n observability deploy/prometheus -- wget -qO- 'http://localhost:9090/api/v1/query?query=prometheus_remote_storage_pending_samples'
kubectl exec -n observability deploy/prometheus -- wget -qO- 'http://localhost:9090/metrics'
kubectl exec -n observability deploy/prometheus -- wget -qO- 'http://localhost:9090/api/v1/query?query={__name__=~"prometheus_remote_storage.*"}'
kubectl port-forward -n observability svc/prometheus 9090:9090
kubectl logs -n observability deploy/prometheus --tail=100
kubectl logs -n observability deploy/prometheus --tail=200
kubectl exec -n observability deploy/prometheus -- cat /etc/prometheus/prometheus.yml
kubectl exec -n observability deploy/prometheus -- ls -la /etc/prometheus/openobserve-auth/
kubectl exec -n observability deploy/prometheus -- cat /etc/prometheus/openobserve-auth/password
```

## Diagnosing the OpenObserve `MemoryTableOverflowError`

```bash
kubectl logs -n observability openobserve-0 --tail=100
kubectl top pod -n observability openobserve-0
kubectl get pod -n observability openobserve-0 -o jsonpath='{.status.containerStatuses[0].restartCount}'
kubectl describe pod -n observability openobserve-0
kubectl logs -n observability openobserve-0 --tail=300
kubectl logs -n observability openobserve-0 --tail=30
kubectl logs -n observability openobserve-0
kubectl exec -n observability openobserve-0 -- df -h /data
kubectl exec -n observability openobserve-0 -- du -sh /data/*
```

## Applying the fix (scoped `write_relabel_configs`) and recovering

```bash
kubectl apply -k /Users/miqui/development/k8s-fastapi-o2/k8s/observability/
kubectl rollout restart deployment/prometheus -n observability
kubectl rollout status deployment/prometheus -n observability --timeout=90s
kubectl delete pod -n observability openobserve-0
kubectl rollout status statefulset/openobserve -n observability --timeout=120s
kubectl port-forward -n observability svc/prometheus 9090:9090
```

## Final verification

```bash
kubectl port-forward -n observability svc/openobserve 5080:5080
kubectl get pods -n observability -o wide
```

## Diagnosing and fixing Prometheus probe timeouts (context deadline exceeded)

```bash
# Inspect pod events and probe failures
kubectl describe pod -n observability -l app=prometheus
kubectl describe -n observability pod/<pod-name>

# Check Prometheus resource usage during scrape and remote-write loads
kubectl top pod -n observability -l app=prometheus

# Apply updated deployment with increased probe timeouts (timeoutSeconds: 3, failureThreshold: 3)
kubectl apply -f k8s/observability/prometheus-deployment.yaml
# or via Kustomize:
kubectl apply -k k8s/observability/

# Monitor rollout
kubectl rollout status deployment/prometheus -n observability

# Verify new pod status and active probe settings
kubectl get pods -n observability -l app=prometheus -o wide
kubectl describe pod -n observability -l app=prometheus
```

## Diagnosing Grafana vs Headlamp pod count mismatch (48 vs 50)

```bash
kubectl config current-context
kubectl get pods --all-namespaces --no-headers | wc -l
kubectl get pods --all-namespaces -o wide
```

Turned out both counts were correct: `kubectl`/Headlamp count all 50 pod objects, while Grafana's
panel filters on `phase="Running"`, excluding the 2 `Completed` `ingress-nginx-admission-*` Job pods
(50 − 2 = 48).

## Diagnosing `argocd-repo-server` liveness probe failures

Symptom: `Liveness probe failed: Get "http://<ip>:8084/healthz?full=true": context deadline exceeded`
and a few restarts (exit code 0). The pod was otherwise healthy (about 2m CPU / 38Mi, no resource limits).

```bash
kubectl get pod -n argocd -l app.kubernetes.io/name=argocd-repo-server -o wide   # RESTARTS column
kubectl describe pod -n argocd -l app.kubernetes.io/name=argocd-repo-server       # probe settings, Last State, Events
kubectl get events -n argocd --sort-by=.lastTimestamp
kubectl top pod -n argocd
```

Reading the repo-server logs (JSON; address the Deployment so it survives pod renames):

```bash
kubectl logs -n argocd deploy/argocd-repo-server
kubectl logs -n argocd deploy/argocd-repo-server -f --tail=50
kubectl logs -n argocd deploy/argocd-repo-server --previous                       # before a liveness restart
kubectl logs -n argocd deploy/argocd-repo-server --since=1h
kubectl logs -n argocd deploy/argocd-repo-server | grep '"level":"error"'
kubectl logs -n argocd deploy/argocd-repo-server | grep -v 'grpc.health.v1.Health' # drop health-check noise
kubectl logs -n argocd deploy/argocd-repo-server | grep healthcheck               # the probe-side failures
kubectl logs -n argocd deploy/argocd-repo-server --since=1h | jq -r '[.time,.level,.msg] | @tsv'
```

The tell-tale line is `Error serving health check request ... context canceled` with
`"duration":5004874461`: the health check normally answers in about 1ms, and here it ran into the
probe's `timeoutSeconds: 5`. That points at a stall around the process (CPU or memory contention in
the Docker VM), not at repo-server being slow.

Checking the Docker Desktop VM (kind runs inside it):

```bash
docker info | grep -E 'CPUs|Total Memory'
docker stats --no-stream
docker run --rm --privileged --pid=host alpine sh -c 'cat /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io; free -m'
kubectl top nodes
```

In the PSI output, `some avg60` above roughly 10% on `cpu` means tasks are regularly waiting for CPU,
and swap in use on the `free -m` line means pages are being swapped out. Both were true here
(CPU `some` ~17%, ~465 MB swapped). The fix is more VM memory (Docker Desktop → Settings →
Resources) and/or fewer kind workers.

Restarts were frequent enough to be a nuisance, so `deploy-kind.sh` now applies this patch right
after installing ArgoCD (alongside the `argocd-server --insecure` patch): it loosens the liveness
probe (about 60s of tolerated stall instead of about 15s) and gives the container a CPU/memory
request, moving it from BestEffort to Burstable QoS so it's less likely to be starved in the first
place:

```bash
kubectl patch deployment argocd-repo-server -n argocd --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/timeoutSeconds","value":10},
  {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/failureThreshold","value":6},
  {"op":"add","path":"/spec/template/spec/containers/0/resources","value":{"requests":{"cpu":"100m","memory":"256Mi"}}}
]'
```

This treats the symptom, not the cause - the underlying fix is still more Docker Desktop VM memory
(Settings → Resources) and/or fewer kind workers if PSI shows real contention.
