#!/bin/bash
# SSH to master, run a probe.
set -euo pipefail
MASTER_IP=13.215.29.213
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 \
    -i /tmp/elastikube-test.pem "ubuntu@${MASTER_IP}" \
    'uname -a; cat /etc/os-release | head -3; ls /var/log/cloud-init-output.log 2>&1 | head -1'
