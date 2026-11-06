#!/bin/bash
set -euo pipefail
ANSIBLE_CONFIG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg
ANSIBLE_CONFIG=$ANSIBLE_CONFIG ansible k3s-master -m shell \
    -a "curl -s -o /dev/null -w 'http_code=%{http_code}\n' --max-time 5 http://archive.ubuntu.com/ ; curl -s -o /dev/null -w 'http_code=%{http_code}\n' --max-time 5 https://github.com" \
    2>&1 | tail -5
