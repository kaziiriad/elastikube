#!/bin/bash
# Install k3s agent on workers via static binary (no apt). Workers are in
# private subnets with no NAT, so apt update fails. We:
#   1. Download the k3s static binary on the master (has internet)
#   2. SCP it to each worker through the master as ProxyJump
#   3. Move the binary to /usr/local/bin/k3s and start it via nohup with
#      --node-label k3s-worker=$NAME

set -euo pipefail
KEY=/tmp/elastikube-test.pem
MASTER_IP=3.0.26.68
K3S_VERSION=v1.36.4+k3s1
ANSIBLE_CFG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg

# Worker inventory
declare -A WORKER_INFO=(
    ["k3s-worker-1"]="10.0.10.36|k3s-worker-1"
    ["k3s-worker-2"]="10.0.10.8|k3s-worker-2"
)

# Fetch join token from master
JOIN_TOKEN=$(ANSIBLE_CONFIG=$ANSIBLE_CFG \
    ansible k3s-master -a 'sudo cat /var/lib/rancher/k3s/server/node-token' --become 2>&1 \
    | tail -3 | grep -v '^k3s-master' | grep -v '^$' | grep -v '^sudo' | tr -d ' \n')
echo "[install-worker] Token length: ${#JOIN_TOKEN}"

# Download k3s static binary on the master
echo "[install-worker] Downloading k3s ${K3S_VERSION} on master"
ANSIBLE_CONFIG=$ANSIBLE_CFG ansible k3s-master -b -m shell \
    -a "curl -sfL https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION}/k3s -o /tmp/k3s && chmod +x /tmp/k3s && ls -la /tmp/k3s" \
    2>&1 | tail -5

# Install on each worker
for worker_alias in k3s-worker-1 k3s-worker-2; do
    IFS='|' read -r worker_ip worker_name <<< "${WORKER_INFO[$worker_alias]}"
    echo ""
    echo "[install-worker] Installing on ${worker_alias} (${worker_ip})"

    # SCP master:/tmp/k3s -> local -> worker
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ProxyCommand="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p -i ${KEY} ubuntu@${MASTER_IP}" \
        -i "${KEY}" \
        "ubuntu@${MASTER_IP}:/tmp/k3s" "/tmp/k3s-${worker_alias}" 2>&1 | tail -2

    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ProxyCommand="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p -i ${KEY} ubuntu@${MASTER_IP}" \
        -i "${KEY}" \
        "/tmp/k3s-${worker_alias}" "ubuntu@${worker_ip}:/tmp/k3s" 2>&1 | tail -2

    # Move to /usr/local/bin + write the k3s env files + start agent
    ANSIBLE_CONFIG=$ANSIBLE_CFG ansible "${worker_alias}" -b -m shell -a "bash -s" <<REMOTE
set -euo pipefail
chmod +x /tmp/k3s
sudo mv /tmp/k3s /usr/local/bin/k3s
sudo mkdir -p /etc/rancher/k3s
echo 'K3S_URL=https://${MASTER_IP}:6443' | sudo tee /etc/rancher/k3s/k3s-url.env >/dev/null
echo 'K3S_TOKEN=${JOIN_TOKEN}' | sudo tee /etc/rancher/k3s/k3s-token.env >/dev/null
sudo chmod 600 /etc/rancher/k3s/k3s-*.env
NODE_NAME='${worker_name}'
nohup sudo /usr/local/bin/k3s agent \\
    --server https://${MASTER_IP}:6443 \\
    --token-file /etc/rancher/k3s/k3s-token.env \\
    --node-label "k3s-worker=\${NODE_NAME}" \\
    >/var/log/k3s-agent.log 2>&1 &
sleep 8
echo "[agent] Started on \${NODE_NAME}"
sudo tail -10 /var/log/k3s-agent.log 2>&1 | head -10
REMOTE
done

echo ""
echo "[install-worker] Waiting 15s for agents to register with master"
sleep 15

echo ""
echo "[install-worker] Verifying nodes:"
ANSIBLE_CONFIG=$ANSIBLE_CFG ansible k3s-master -a 'sudo kubectl get nodes --show-labels' --become 2>&1 | tail -10
