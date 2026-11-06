#!/bin/bash
set -euo pipefail
KEY=/tmp/elastikube-test.pem
MASTER_IP=3.0.26.68
WORKER_IP=10.0.10.36
scp -v -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ProxyCommand="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p -i ${KEY} ubuntu@${MASTER_IP}" \
    -i "${KEY}" \
    /tmp/k3s-k3s-worker-1 ubuntu@${WORKER_IP}:/tmp/k3s 2>&1 | tail -25
