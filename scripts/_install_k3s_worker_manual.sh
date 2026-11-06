#!/bin/bash
# Install k3s agent on the workers WITHOUT going through apt — workers
# are in a private subnet with no NAT, so apt update fails. We:
#   1. Download the k3s static binary on the master (which has internet)
#   2. SCP it to each worker
#   3. Run the install with --node-label k3s-worker=$NAME on each worker

set -euo pipefail
KEY=/tmp/elastikube-test.pem
MASTER_IP=54.255.250.84
WORKERS=(10.0.10.176 10.0.10.19)
K3S_VERSION=v1.36.4+k3s1
K3S_URL="https://${MASTER_IP}:6443"

# Get the join token from the master
JOIN_TOKEN=$(ANSIBLE_CONFIG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg \
    ansible k3s-master -a 'sudo cat /var/lib/rancher/k3s/server/node-token' --become 2>&1 \
    | tail -3 | grep -v '^k3s-master' | grep -v '^$' | grep -v '^sudo' | tr -d ' \n')
echo "[install-worker] Token length: ${#JOIN_TOKEN}"

# Download k3s binary on master (it has internet)
echo "[install-worker] Downloading k3s ${K3S_VERSION} on master"
ANSIBLE_CONFIG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg \
    ansible k3s-master -b -m shell \
    -a "curl -sfL https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION}/k3s -o /tmp/k3s && chmod +x /tmp/k3s && ls -la /tmp/k3s" 2>&1 | tail -5

# Worker inventory: hostname -> (ip, name_tag)
declare -A WORKER_INFO=(
    ["k3s-worker-1"]="10.0.10.176|k3s-worker-1"
    ["k3s-worker-2"]="10.0.10.19|k3s-worker-2"
)

for worker_alias in k3s-worker-1 k3s-worker-2; do
    IFS='|' read -r worker_ip worker_name <<< "${WORKER_INFO[$worker_alias]}"
    echo "[install-worker] Installing on ${worker_alias} (${worker_ip}) as NODE_NAME=${worker_name}"

    # SCP binary from master to worker
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ProxyCommand="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p -i ${KEY} ubuntu@${MASTER_IP}" \
        -i "${KEY}" \
        "ubuntu@${MASTER_IP}:/tmp/k3s" "/tmp/k3s-${worker_alias}"

    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ProxyCommand="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p -i ${KEY} ubuntu@${MASTER_IP}" \
        -i "${KEY}" \
        "/tmp/k3s-${worker_alias}" "ubuntu@${worker_ip}:/tmp/k3s"

    # Install + start k3s agent on the worker
    ANSIBLE_CONFIG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg \
    ansible "${worker_alias}" -b -m shell -a "bash -s" <<REMOTE
set -euo pipefail
chmod +x /tmp/k3s
sudo mv /tmp/k3s /usr/local/bin/k3s
sudo mkdir -p /etc/rancher/k3s
echo 'K3S_URL=${K3S_URL}' | sudo tee /etc/rancher/k3s/k3s-url.env >/dev/null
echo 'K3S_TOKEN=${JOIN_TOKEN}' | sudo tee /etc/rancher/k3s/k3s-token.env >/dev/null
sudo chmod 600 /etc/rancher/k3s/k3s-*.env

# nohup backgrounds the agent (no systemd on these AMIs per CLAUDE.md)
NODE_NAME="${worker_name}"
nohup sudo /usr/local/bin/k3s agent \\
    --server '${K3S_URL}' \\
    --token-file /etc/rancher/k3s/k3s-token.env \\
    --node-label "k3s-worker=\${NODE_NAME}" \\
    >/var/log/k3s-agent.log 2>&1 &
sleep 8
echo "[agent] Started on \${NODE_NAME}"
sudo tail -5 /var/log/k3s-agent.log 2>&1 | head -5
REMOTE
done

echo "[install-worker] Waiting 10s for agents to register with master"
sleep 10

echo "[install-worker] Verifying nodes from master:"
ANSIBLE_CONFIG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg \
    ansible k3s-master -a 'sudo kubectl get nodes --show-labels' --become 2>&1 | tail -15
