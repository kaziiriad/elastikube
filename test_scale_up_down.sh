#!/bin/bash
# Test scale-up and scale-down by deploying pending pods

set -e

KUBECONFIG="${1:-/mnt/e/custom_autoscaler/production/cluster/kubeconfig}"

echo "=== K3s Scale-Up/Down Test ==="
echo ""

# Check current nodes
echo "1. Current cluster state:"
kubectl --kubeconfig="$KUBECONFIG" get nodes -o wide
echo ""

# Deploy pending-pod workload to trigger scale-up
echo "2. Creating pending-pod workload (triggers scale-up)..."
cat <<EOF | kubectl --kubeconfig="$KUBECONFIG" apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pending-pod-test
  namespace: default
spec:
  replicas: 10
  selector:
    matchLabels:
      app: stress-test
  template:
    metadata:
      labels:
        app: stress-test
    spec:
      nodeSelector:
        non-existing: "true"  # Forces pods to be pending
      containers:
      - name: pause
        image: registry.k8s.io/e2e-test-images/pause:3.9
EOF

echo ""

# Wait for Lambda to trigger scale-up
echo "3. Waiting for autoscaler to scale up (2-3 minutes)..."
echo "   Monitor: aws logs tail /aws/lambda/k3s-autoscaler-function-64ea403 --follow"
sleep 180

# Check new node
echo "4. Checking for new node..."
kubectl --kubeconfig="$KUBECONFIG" get nodes -o wide
echo ""

# Remove the workload to allow scale-down
echo "5. Removing pending-pod workload..."
kubectl --kubeconfig="$KUBECONFIG" delete deployment pending-pod-test
echo ""

# Wait for cooldown and scale-down
echo "6. Waiting for scale-down (15 min cooldown)..."
echo "   Monitor: aws logs tail /aws/lambda/k3s-autoscaler-function-64ea403 --follow"
sleep 900

# Final state
echo "7. Final cluster state:"
kubectl --kubeconfig="$KUBECONFIG" get nodes -o wide
echo ""

echo "=== Test Complete ==="
echo ""
echo "Check Lambda logs for drain execution:"
echo "  aws logs tail /aws/lambda/k3s-autoscaler-function-64ea403 --follow | grep -i drain"
