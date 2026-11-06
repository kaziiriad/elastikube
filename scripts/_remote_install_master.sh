#!/bin/bash
set -euo pipefail
PUB_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)
echo "[install-k3s] Public IP: $PUB_IP"

if command -v k3s >/dev/null 2>&1; then
    echo "[install-k3s] k3s already installed: $(k3s --version 2>&1 | head -1)"
else
    echo "[install-k3s] Downloading + running k3s installer"
    curl -sfL https://get.k3s.io | \
        INSTALL_K3S_EXEC="server --disable=traefik --disable=servicelb --tls-san=$PUB_IP" \
        sh -
    echo "[install-k3s] k3s installed"
fi

# Wait for the cluster to be Ready
echo "[install-k3s] Waiting for k3s to become Ready..."
for i in $(seq 1 30); do
    if sudo k3s kubectl get nodes 2>/dev/null | grep -q " Ready "; then
        echo "[install-k3s] k3s Ready after ${i}s"
        break
    fi
    sleep 2
done

# Copy kubeconfig to ubuntu user so ansible + kubectl don't need sudo
sudo cp /etc/rancher/k3s/k3s.yaml /home/ubuntu/k3s.yaml
sudo chown ubuntu:ubuntu /home/ubuntu/k3s.yaml
sudo chmod 600 /home/ubuntu/k3s.yaml

# Install k8s python client for the kubernetes.core Ansible module
if ! python3 -c 'import kubernetes' >/dev/null 2>&1; then
    echo "[install-k3s] Installing kubernetes python client"
    curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    sudo python3 /tmp/get-pip.py --break-system-packages
    sudo python3 -m pip install kubernetes --break-system-packages
else
    echo "[install-k3s] kubernetes client already installed"
fi

echo "[install-k3s] Done. Cluster info:"
sudo k3s kubectl get nodes 2>&1
