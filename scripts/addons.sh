#!/bin/bash
# Cluster add-ons, run on k8s-master once all nodes have joined:
#   local-path storage, metrics-server, MetalLB, Traefik (ingress), kube-prometheus-stack.
# Idempotent (helm upgrade --install); skipped when already done unless FORCE=1.
set -euo pipefail
LB_RANGE="$1"
FORCE="${2:-0}"
export KUBECONFIG=/etc/kubernetes/admin.conf

if [ "$FORCE" != "1" ] && kubectl -n kube-system get configmap addons-installed >/dev/null 2>&1; then
  echo "Add-ons already installed (ADDONS_FORCE=1 vagrant provision to reinstall)"
  exit 0
fi

step() { echo; echo "===== $* ====="; }

if ! command -v helm >/dev/null; then
  step "Installing Helm"
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4 | bash
fi

kubectl wait --for=condition=Ready nodes --all --timeout=300s

helm repo add metallb https://metallb.github.io/metallb --force-update
helm repo add traefik https://traefik.github.io/charts --force-update
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
helm repo update

step "Storage: local-path-provisioner (default StorageClass)"
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.37/deploy/local-path-storage.yaml
kubectl patch storageclass local-path \
  -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'

step "metrics-server (kubectl top)"
# kubelet serving certs are self-signed in a kubeadm lab
helm upgrade --install metrics-server metrics-server/metrics-server -n kube-system \
  --set 'args={--kubelet-insecure-tls}' --wait

step "MetalLB (LoadBalancer IPs ${LB_RANGE})"
helm upgrade --install metallb metallb/metallb -n metallb-system --create-namespace --wait
# The admission webhook can take a few seconds after the pods are Ready
for i in $(seq 1 30); do
  kubectl apply -f - <<EOF && break
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: lab-pool
  namespace: metallb-system
spec:
  addresses:
    - ${LB_RANGE}
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: lab-l2
  namespace: metallb-system
spec:
  ipAddressPools:
    - lab-pool
EOF
  echo "MetalLB webhook not ready yet, retrying ($i)"; sleep 5
done

step "Traefik (default IngressClass, LoadBalancer service)"
helm upgrade --install traefik traefik/traefik -n traefik --create-namespace \
  --set ingressClass.isDefaultClass=true \
  --set service.type=LoadBalancer \
  --wait

step "Prometheus + Grafana (kube-prometheus-stack)"
cat > /tmp/monitoring-values.yaml <<'EOF'
grafana:
  service:
    type: LoadBalancer
    port: 80
prometheus:
  prometheusSpec:
    retention: 3d
    resources:
      requests: { cpu: 100m, memory: 400Mi }
      limits: { memory: 1200Mi }
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: local-path
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 5Gi
alertmanager:
  alertmanagerSpec:
    resources:
      requests: { cpu: 10m, memory: 50Mi }
      limits: { memory: 150Mi }
# kubeadm binds these to 127.0.0.1, so scraping them only produces false alerts
kubeEtcd:
  enabled: false
kubeControllerManager:
  enabled: false
kubeScheduler:
  enabled: false
kubeProxy:
  enabled: false
EOF
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f /tmp/monitoring-values.yaml --wait --timeout 15m

kubectl -n kube-system create configmap addons-installed \
  --from-literal=date="$(date -Is)" --dry-run=client -o yaml | kubectl apply -f -

step "Done"
kubectl get svc -A --field-selector spec.type=LoadBalancer
echo
echo "Grafana user: admin  password: $(kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 -d)"
