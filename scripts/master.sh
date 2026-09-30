#!/bin/bash
# Control plane: kubeadm init + Flannel CNI.
set -euo pipefail
POD_CIDR="$1"

if [ -f /etc/kubernetes/admin.conf ]; then
  echo "Control plane already initialised, skipping kubeadm init"
  exit 0
fi

NODE_IP=$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | grep -v '^10\.0\.2\.' | head -1)
IFACE=$(ip -4 -o addr show | awk -v ip="$NODE_IP" '$4 ~ "^"ip"/" {print $2}')

kubeadm config images pull
kubeadm init \
  --apiserver-advertise-address="$NODE_IP" \
  --apiserver-cert-extra-sans="$NODE_IP,127.0.0.1,localhost" \
  --pod-network-cidr="$POD_CIDR" \
  --node-name "$(hostname)"

# kubectl for the vagrant user
mkdir -p /home/vagrant/.kube
cp /etc/kubernetes/admin.conf /home/vagrant/.kube/config
chown -R vagrant:vagrant /home/vagrant/.kube
echo 'source <(kubectl completion bash); alias k=kubectl; complete -o default -F __start_kubectl k' >> /home/vagrant/.bashrc

# Flannel, pinned to the Shared-network interface (every node has the same NIC layout)
export KUBECONFIG=/etc/kubernetes/admin.conf
curl -fsSL https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml \
  | sed "s|- --kube-subnet-mgr|- --kube-subnet-mgr\n        - --iface=${IFACE}|" \
  | sed "s|\"Network\": \"10.244.0.0/16\"|\"Network\": \"${POD_CIDR}\"|" \
  | kubectl apply -f -

echo "Control plane ready on ${NODE_IP} (${IFACE})"
