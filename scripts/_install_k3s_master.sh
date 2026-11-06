#!/bin/bash
# Install k3s server on the master node.
set -euo pipefail
MASTER_IP=13.215.29.213
PUB_IP=$(curl -s --max-time 5 http://169.254.169.254/latest/meta-data/public-ipv4)

# Render the install command (uses remote curl for the public IP)
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -i /tmp/elastikube-test.pem "ubuntu@${MASTER_IP}" \
    bash -s <<REMOTE
set -euo pipefail
PUB_IP=\$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)
echo "[install-k3s] Public IP: \$PUB_IP"

if command -v k3s >/dev/null 2>&1; then
    echo "[install-k3s] k3s already installed: \$(k3s --version 2>&1 | head -1)"
else
    echo "[install-k3s] Downloading + running k3s installer"
    curl -sfL https://get.k3s.io | \
        INSTALL_K3S_EXEC="server --disable=traefik --disable=servicelb --tls-san=\$PUB_IP" \
        sh -
    echo "[install-k3s] k3s installed"
fi

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
kubectl get nodes --kubeconfig /home/ubuntu/k3s.yaml 2>&1 | head -5
REMOTE
