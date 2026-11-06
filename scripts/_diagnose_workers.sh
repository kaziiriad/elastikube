#!/bin/bash
# Diagnose k3s-agent on each worker.
set -euo pipefail
ANSIBLE_CONFIG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg
for w in k3s-worker-1 k3s-worker-2; do
    echo "=== $w: agent log ==="
    ANSIBLE_CONFIG=$ANSIBLE_CONFIG ansible "$w" -b -m shell \
        -a "tail -30 /var/log/k3s-agent.log 2>&1" 2>&1 | tail -15
    echo ""
    echo "=== $w: processes ==="
    ANSIBLE_CONFIG=$ANSIBLE_CONFIG ansible "$w" -b -m shell \
        -a "ps -ef | grep k3s | grep -v grep" 2>&1 | tail -8
    echo ""
    echo "=== $w: listening ports ==="
    ANSIBLE_CONFIG=$ANSIBLE_CONFIG ansible "$w" -b -m shell \
        -a "ss -lntp 2>/dev/null | head -20" 2>&1 | tail -15
    echo ""
done
