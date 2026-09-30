#!/bin/bash
# Runs on every node: kernel prep, containerd, kubeadm/kubelet/kubectl.
set -euo pipefail
K8S_VERSION="$1"
export DEBIAN_FRONTEND=noninteractive

# Every VM is cloned from the same box, so they share /etc/machine-id. systemd-networkd
# derives its DHCP client-id from it, so all nodes got the SAME IP. Give each node its own
# machine-id and request the lease by MAC instead.
if [ ! -f /etc/.k8s-identity-reset ]; then
  rm -f /etc/machine-id /var/lib/dbus/machine-id
  systemd-machine-id-setup
  cat > /etc/netplan/60-dhcp-by-mac.yaml <<EOF
network:
  version: 2
  ethernets:
    enp0s1:
      dhcp4: true
      dhcp-identifier: mac
EOF
  chmod 600 /etc/netplan/60-dhcp-by-mac.yaml
  netplan apply
  touch /etc/.k8s-identity-reset
  sleep 5
fi

# Node IP = address on the UTM Shared network (the NIC that is not the 10.0.2.x emulated NAT)
NODE_IP=$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | grep -v '^10\.0\.2\.' | head -1)
echo "Node IP: $NODE_IP"

# --- swap off, kernel modules, sysctl
swapoff -a
sed -i '/\sswap\s/ s/^#*/#/' /etc/fstab
cat > /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter
cat > /etc/sysctl.d/99-k8s.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system >/dev/null

# --- containerd (systemd cgroups)
apt-get update -y
apt-get install -y containerd apt-transport-https ca-certificates curl gpg
mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
systemctl restart containerd
systemctl enable containerd

# --- kubeadm, kubelet, kubectl
mkdir -p /etc/apt/keyrings
curl -fsSL "https://pkgs.k8s.io/core:/stable:/v${K8S_VERSION}/deb/Release.key" \
  | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v${K8S_VERSION}/deb/ /" \
  > /etc/apt/sources.list.d/kubernetes.list
apt-get update -y
apt-get install -y kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl

# kubelet must advertise the Shared-network IP, not the emulated NAT IP
echo "KUBELET_EXTRA_ARGS=--node-ip=${NODE_IP}" > /etc/default/kubelet
systemctl enable --now kubelet
