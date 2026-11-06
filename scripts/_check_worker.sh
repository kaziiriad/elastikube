#!/bin/bash
set -euo pipefail
ANSIBLE_CONFIG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg
echo "=== Ansible to k3s-worker-1 ==="
ANSIBLE_CONFIG=$ANSIBLE_CONFIG ansible k3s-worker-1 -m shell \
    -a "hostname -i; ls -la /tmp/" 2>&1 | tail -15
