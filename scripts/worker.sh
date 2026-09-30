#!/bin/bash
# Worker: join the cluster using the command exported from k8s-master.
set -euo pipefail

if [ -f /etc/kubernetes/kubelet.conf ]; then
  echo "Already joined, skipping"
  exit 0
fi
if [ ! -f /vagrant/join.sh ]; then
  echo "ERROR: /vagrant/join.sh missing - bring up k8s-master first (vagrant up k8s-master)" >&2
  exit 1
fi

bash /vagrant/join.sh --node-name "$(hostname)"
