#!/bin/bash
# SCP the install script to the master, run it, then verify.
set -euo pipefail
MASTER_IP=13.215.29.213
KEY=/tmp/elastikube-test.pem
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -i "$KEY" \
    /mnt/e/custom_autoscaler/production/scripts/_remote_install_master.sh \
    "ubuntu@${MASTER_IP}:/tmp/install-master.sh"
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -i "$KEY" \
    "ubuntu@${MASTER_IP}" \
    "chmod +x /tmp/install-master.sh && sudo bash /tmp/install-master.sh"
