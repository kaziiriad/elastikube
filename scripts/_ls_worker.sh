#!/bin/bash
set -euo pipefail
ANSIBLE_CONFIG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg
ANSIBLE_CONFIG=$ANSIBLE_CONFIG ansible k3s-worker-1 -b -m shell \
    -a "ls -la /usr/local/bin/k3s /etc/rancher/k3s/ /var/log/k3s-agent.log 2>&1; cat /etc/rancher/k3s/k3s-token.env 2>&1" \
    2>&1 | tail -15
