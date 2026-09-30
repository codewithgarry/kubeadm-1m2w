#!/usr/bin/env bash
# Installs the whole lab, in dependency order, validating each step. Idempotent - safe to re-run.
# Does NOT touch the existing kube-prometheus-stack release ("monitoring"): it only adds a
# ServiceMonitor (observability ns), a datasource ConfigMap and a dashboard ConfigMap.
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n\033[1;32m### %s\033[0m\n' "$*"; }

step "Helm repositories"
helm repo add grafana-community https://grafana-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
helm repo update grafana-community grafana open-telemetry >/dev/null

step "Namespaces"
kubectl apply -f 00-namespaces.yaml

step "1/7 Loki (logs backend)"
helm upgrade --install loki grafana-community/loki --version 18.13.7 -n observability -f helm/loki-values.yaml --wait --timeout 10m
kubectl -n observability rollout status sts/loki --timeout=5m

step "2/7 Tempo (traces backend)"
helm upgrade --install tempo grafana-community/tempo --version 3.0.0 -n observability -f helm/tempo-values.yaml --wait --timeout 10m

step "3/7 OpenTelemetry Collector (gateway)"
helm upgrade --install otel-collector open-telemetry/opentelemetry-collector --version 0.174.0 -n observability -f helm/otel-collector-values.yaml --wait --timeout 5m

step "4/7 Grafana Alloy (Kubernetes container logs)"
helm upgrade --install alloy grafana/alloy --version 1.13.0 -n observability -f helm/alloy-values.yaml --wait --timeout 5m

step "5/7 PostgreSQL + toxiproxy"
kubectl apply -f apps/postgres.yaml -f apps/toxiproxy.yaml
kubectl -n petclinic rollout status sts/postgres --timeout=5m
kubectl -n petclinic rollout status deploy/toxiproxy --timeout=2m

step "6/7 PetClinic (build jar once, then run with the OTel Java agent) + load generator"
kubectl apply -f apps/petclinic-build.yaml
if ! kubectl -n petclinic get job petclinic-build -o jsonpath='{.status.succeeded}' 2>/dev/null | grep -q 1; then
  kubectl -n petclinic wait --for=condition=complete job/petclinic-build --timeout=15m
fi
kubectl apply -f apps/petclinic.yaml
kubectl -n petclinic rollout status deploy/petclinic --timeout=6m
kubectl apply -k apps/loadgen
kubectl -n petclinic rollout status deploy/petclinic-loadgen --timeout=6m

step "7/7 Grafana datasources + dashboard (existing Grafana)"
kubectl apply -f grafana/datasources.yaml
python3 grafana/generate_dashboard.py
kubectl apply -f grafana/dashboard-java-lab.yaml
cat <<'EOF'
NOTE: Grafana hot-reloads datasources through its admin API. In this cluster the sidecar's
      credentials (Helm secret) no longer match the admin password, so run ONE of:
        ./scripts/lab.sh grafana-reload        # asks for your current Grafana password
      or restart Grafana (loses UI-made changes - its storage is an emptyDir):
        kubectl -n monitoring rollout restart deploy/monitoring-grafana
EOF

step "Verification (give it ~2 minutes of traffic first)"
echo "./scripts/lab.sh verify"
