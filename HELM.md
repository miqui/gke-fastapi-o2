# Helm Commands — OpenObserve & Headlamp Rollout

`helm` commands used while adding OpenObserve (an observability backend) and Headlamp (a
Kubernetes dashboard), plus their supporting configuration, grouped by phase.

## Chart discovery (finding the right chart/repo before using it)

```bash
helm repo list
helm search hub openobserve
helm search hub openobserve --output json
helm repo add openobserve https://charts.openobserve.ai
helm repo update openobserve
helm show values openobserve/openobserve-standalone
helm show chart openobserve/openobserve-standalone
helm pull openobserve/openobserve-standalone --untar --untardir <scratch-dir>
```

## From `deploy-kind.sh` (the automated deployment)

```bash
# idempotent repo add - only runs if not already registered
if ! helm repo list | grep -q '^openobserve[[:space:]]'; then
  helm repo add openobserve https://charts.openobserve.ai
fi
helm repo update openobserve
helm upgrade --install openobserve openobserve/openobserve-standalone \
  --version 0.92.2 \
  --namespace observability \
  -f k8s/observability/openobserve-values.yaml \
  --wait --timeout 180s
```

## Chart discovery — Headlamp

```bash
helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/
helm repo update headlamp
helm show values headlamp/headlamp --version 0.45.0
```

## From `deploy-kind.sh` — Headlamp

```bash
if ! helm repo list | grep -q '^headlamp[[:space:]]'; then
  helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/
fi
helm repo update headlamp
helm upgrade --install headlamp headlamp/headlamp \
  --version 0.45.0 \
  --namespace headlamp \
  --create-namespace \
  -f k8s/headlamp/headlamp-values.yaml \
  --wait --timeout 120s
```

## Manual verification

```bash
helm list -n observability
helm list -n headlamp
```

## Manual re-applies after values changes

Each of the following config changes to `k8s/observability/openobserve-values.yaml` was applied
live with the same `helm upgrade --install` command (the chart's `checksum/config` annotation on
the StatefulSet's pod template forces an automatic rollout on each upgrade, so no manual restart
was needed):

- Retention cap (`config.ZO_COMPACT_DATA_RETENTION_DAYS: "3"`, down from the chart's 3650-day default)
- Resource bump (`resources.requests`/`resources.limits`, before widening remote_write to cluster ops metrics)
- Self-metrics enabled (`config.ZO_PROMETHEUS_ENABLED: "true"`, for the OpenObserve Ops Grafana dashboard)

```bash
helm upgrade --install openobserve openobserve/openobserve-standalone \
  --version 0.92.2 \
  --namespace observability \
  -f k8s/observability/openobserve-values.yaml \
  --wait --timeout 180s
```
