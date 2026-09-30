# -*- mode: ruby -*-
# vi: set ft=ruby :
#
# 3-node Kubernetes cluster (1 control plane + 2 workers) on Apple Silicon,
# using UTM + vagrant_utm, Ubuntu 24.04 arm64, containerd, kubeadm and Flannel.
#
#   vagrant up                 # builds k8s-master, then node-1, node-2 (in order)
#   vagrant ssh k8s-master     # kubectl get nodes
#   kubectl --kubeconfig ./kubeconfig get nodes   # from the Mac
#   vagrant halt | vagrant destroy -f
#
# Networking: NIC 1 = UTM "Shared" network (192.168.64.0/24, DHCP) used for all
# node-to-node / API traffic; NIC 2 = UTM "Emulated" NAT used only by Vagrant SSH.

ENV["VAGRANT_DEFAULT_PROVIDER"] = "utm"

BOX           = "utm/ubuntu-24.04"
K8S_VERSION   = "1.37"           # pkgs.k8s.io minor stream
POD_CIDR      = "10.244.0.0/16"  # must not overlap the UTM network (192.168.64.0/24)
WORKERS       = 2
MASTER_CPUS   = 2
MASTER_MEMORY = 4096
WORKER_CPUS   = 2
WORKER_MEMORY = 4096             # 4 GB so Prometheus/Grafana fit comfortably

# Add-ons installed once all workers have joined (scripts/addons.sh)
ADDONS        = true
# MetalLB hands out LoadBalancer IPs from this slice of the UTM Shared network.
# macOS DHCP hands out leases upward from .2, so the top of the range stays free.
LB_RANGE      = "192.168.64.100-192.168.64.250"

Vagrant.configure("2") do |config|
  config.vm.box = BOX
  # Only this folder (scripts + generated join.sh) goes into the guests
  config.vm.synced_folder ".", "/vagrant", type: "rsync",
    rsync__exclude: [".vagrant/", "kubeconfig"]

  config.vm.provision "common", type: "shell",
    path: "scripts/common.sh", args: [K8S_VERSION]

  config.vm.define "k8s-master", primary: true do |m|
    m.vm.hostname = "k8s-master"
    m.vm.network "forwarded_port", guest: 6443, host: 6443, host_ip: "127.0.0.1", id: "k8s-api"

    m.vm.provider "utm" do |u|
      u.name   = "k8s-master"
      u.cpus   = MASTER_CPUS
      u.memory = MASTER_MEMORY
      u.directory_share_mode = "none"
    end

    m.vm.provision "master", type: "shell", path: "scripts/master.sh", args: [POD_CIDR]

    # After the master is up, pull the join command and kubeconfig back to the Mac
    # so the workers (which rsync this folder at boot) can join.
    m.trigger.after [:up, :provision] do |t|
      t.info = "Exporting join command and kubeconfig from k8s-master"
      t.ruby do |env, machine|
        read = lambda do |cmd|
          out = +""
          machine.communicate.sudo(cmd) { |type, data| out << data if type == :stdout }
          out
        end
        dir = File.dirname(__FILE__)
        File.write(File.join(dir, "join.sh"),
                   "#!/bin/bash\n" + read.call("kubeadm token create --print-join-command --ttl 24h").strip + ' "$@"' + "\n")
        File.write(File.join(dir, "kubeconfig"), read.call("cat /etc/kubernetes/admin.conf"))
      end
    end
  end

  (1..WORKERS).each do |i|
    config.vm.define "node-#{i}" do |n|
      n.vm.hostname = "node-#{i}"

      n.vm.provider "utm" do |u|
        u.name   = "node-#{i}"
        u.cpus   = WORKER_CPUS
        u.memory = WORKER_MEMORY
        u.directory_share_mode = "none"
      end

      n.vm.provision "worker", type: "shell", path: "scripts/worker.sh"

      # Once the last worker is in, install add-ons from the master
      if ADDONS && i == WORKERS
        n.trigger.after [:up, :provision] do |t|
          t.info = "Installing cluster add-ons from k8s-master"
          t.ruby do |env, _machine|
            master = env.machine(:"k8s-master", :utm)
            force  = ENV["ADDONS_FORCE"] == "1" ? "1" : "0"
            master.communicate.sudo("bash /vagrant/scripts/addons.sh #{LB_RANGE} #{force}") do |_type, data|
              env.ui.info(data.chomp, prefix: false)
            end
          end
        end
      end
    end
  end
end
