#!/bin/bash
set -euo pipefail
ANSIBLE_CFG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg
MASTER_IP=3.0.26.68
JOIN_TOKEN=$(ANSIBLE_CONFIG=$ANSIBLE_CFG \
    ansible k3s-master -a 'sudo cat /var/lib/rancher/k3s/server/node-token' --become 2>&1 \
    | tail -3 | grep -v '^k3s-master' | grep -v '^$' | grep -v '^sudo' | tr -d ' \n')
echo "[install-worker] Token length: ${#JOIN_TOKEN}"

declare -A WORKER_INFO=(
    ["k3s-worker-1"]="10.0.10.36|k3s-worker-1"
    ["k3s-worker-2"]="10.0.10.8|k3s-worker-2"
)

for worker_alias in k3s-worker-1 k3s-worker-2; do
    IFS='|' read -r worker_ip worker_name <<< "${WORKER_INFO[$worker_alias]}"
    echo ""
    echo "[install-worker] Installing $worker_alias ($worker_ip)"

    # Step 1: move binary to /usr/local/bin/k3s
    ANSIBLE_CONFIG=$ANSIBLE_CFG ansible "$worker_alias" -b -m shell \
        -a "chmod +x /tmp/k3s && sudo mv /tmp/k3s /usr/local/bin/k3s && ls -la /usr/local/bin/k3s" \
        2>&1 | tail -5

    # Step 2: write env files
    ANSIBLE_CONFIG=$ANSIBLE_CFG ansible "$worker_alias" -b -m shell \
        -a "sudo mkdir -p /etc/rancher/k3s && echo 'K3S_URL=https://${MASTER_IP}:6443' | sudo tee /etc/rancher/k3s/k3s-url.env && echo 'K3S_TOKEN=${JOIN_TOKEN}' | sudo tee /etc/rancher/k3s/k3s-token.env && sudo chmod 600 /etc/rancher/k3s/k3s-*.env && ls -la /etc/rancher/k3s/" \
        2>&1 | tail -8

    # Step 3: launch agent (use raw module with explicit nohup + disown so the
    # Ansible task returns without blocking)
    ANSIBLE_CONFIG=$ANSIBLE_CFG ansible "$worker_alias" -b -m shell \
        -a "nohup sudo /usr/local/bin/k3s agent --server https://${MASTER_IP}:6443 --token-file /etc/rancher/k3s/k3s-token.env --node-label 'k3s-worker=${worker_name}' >/var/log/k3s-agent.log 2>&1 < /dev/null & disown; sleep 3; ps -ef | grep k3s-agent | grep -v grep | head -3; ls -la /var/log/k3s-agent.log" \
        2>&1 | tail -10
done

echo ""
echo "[install-worker] Waiting 30s for agents to register"
sleep 30

echo ""
echo "[install-worker] Cluster nodes:"
ANSIBLE_CONFIG=$ANSIBLE_CFG ansible k3s-master -a 'sudo kubectl get nodes --show-labels' --become 2>&1 | tail -10
