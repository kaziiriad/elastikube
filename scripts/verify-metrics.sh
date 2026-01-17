#!/bin/bash
# verify-metrics.sh

echo "=== Checking Prometheus Metrics ==="
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
PF_PID=$!
sleep 3

# Test node-exporter metrics
curl -s http://localhost:9090/api/v1/query?query=node_cpu_seconds_total | jq '.data.result | length'

# Test kube-state-metrics
curl -s http://localhost:9090/api/v1/query?query=kube_node_status_condition | jq '.data.result | length'

kill $PF_PID

echo -e "\n=== Checking CloudWatch Metrics (requires AWS CLI) ==="
aws cloudwatch list-metrics \
  --namespace ContainerInsights/Prometheus \
  --region ap-southeast-1 \
  --query 'Metrics[*].[MetricName,Dimensions[0].Value]' \
  --output table
