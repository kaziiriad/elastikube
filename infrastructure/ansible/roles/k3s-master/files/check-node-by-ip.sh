#!/bin/bash
# Check if a node with the given IP exists in the K3s cluster
# Used by cleanup Lambda to verify nodes actually joined before terminating
#
# Usage: check-node-by-ip.sh <worker-ip>
# Output: NODE_NAME=<name> and NODE_READY=<True|False|Unknown> or error
#
# Exit codes:
#   0 - Node found
#   1 - Node not found
#   2 - Usage error

set -e

WORKER_IP="${1:-}"

if [ -z "$WORKER_IP" ]; then
    echo "Usage: $0 <worker-ip>" >&2
    exit 2
fi

# Find node by IP
NODES=$(sudo kubectl get nodes -o custom-columns=NAME:.metadata.name --no-headers 2>/dev/null || true)

for NODE in $NODES; do
    NODE_IP=$(sudo kubectl get node "$NODE" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || echo "")

    if [ "$NODE_IP" = "$WORKER_IP" ]; then
        echo "NODE_NAME=$NODE"

        # Check Ready status
        READY=$(sudo kubectl get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
        echo "NODE_READY=$READY"

        if [ "$READY" = "True" ]; then
            echo "NODE_STATUS=ready"
        else
            echo "NODE_STATUS=not_ready"
        fi
        exit 0
    fi
done

# Node not found
echo "NODE_NAME="
echo "NODE_READY=False"
echo "NODE_STATUS=not_found"
exit 1
