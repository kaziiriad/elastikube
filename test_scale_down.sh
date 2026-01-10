#!/bin/bash
# Test scale-down functionality

set -e

AWS_PROFILE="k3s-temp-user"
STATE_TABLE="k3s-cluster-state-92f4f4d"
CLUSTER_ID="production-k3s"

echo "=== K3s Scale-Down Test ==="
echo ""

# Step 1: Show current EC2 instances
echo "1. Current EC2 instances:"
aws ec2 describe-instances \
  --profile $AWS_PROFILE \
  --filters "Name=tag:Name,Values=k3s-worker-*" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].[InstanceId,Tags[?Key==`Name`].Value|[0],LaunchTime]' \
  --output table
echo ""

# Step 2: Show current cluster state
echo "2. Current DynamoDB state:"
aws dynamodb get-item \
  --profile $AWS_PROFILE \
  --table-name $STATE_TABLE \
  --key "{\"cluster_id\":{\"S\":\"$CLUSTER_ID\"}}" \
  --query 'Item.{NodeCount:node_count,LastOperation:last_scale_operation,ScalingInProgress:scaling_in_progress}' \
  --output table
echo ""

# Step 3: Check WAL for incomplete operations
echo "3. Incomplete WAL operations:"
aws dynamodb scan \
  --profile $AWS_PROFILE \
  --table-name k3s-scaling-wal-70bb938 \
  --filter-expression "state = :pending OR state = :in_progress" \
  --expression-attribute-values '{":pending":{"S":"PENDING"},":in_progress":{"S":"IN_PROGRESS"}}' \
  --query 'Items[].{OperationId:operation_id,State:state,Started:started_at}' \
  --output table || echo "No incomplete operations"
echo ""

# Step 4: Show Kubernetes nodes (if you have kubectl access)
echo "4. Kubernetes nodes:"
echo "(Run manually: docker exec k3s-master kubectl get nodes)"
echo ""

# Step 5: Instructions for manual trigger
echo "=== To Trigger Manual Lambda Run ==="
echo "aws lambda invoke --profile $AWS_PROFILE \\"
echo "  --function-name k3s-autoscaler-function-64ea403 \\"
echo "  --payload '{}' response.json && cat response.json"
echo ""

# Step 6: Check if master is accessible for SSM drain
echo "=== SSM Master Access Check ==="
MASTER_IP=$(aws ec2 describe-instances --profile $AWS_PROFILE \
  --filters "Name=tag:Name,Values=k3s-master" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)

if [ -n "$MASTER_IP" ]; then
    echo "Master IP: $MASTER_IP"

    # Check if SSM is available
    SSM_STATUS=$(aws ssm describe-instance-information --profile $AWS_PROFILE \
      --filters "Key=IPAddress,Values=$MASTER_IP" \
      --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || echo "Not found")

    echo "SSM Agent Status: $SSM_STATUS"
else
    echo "Master instance not found or not accessible"
fi
echo ""

echo "=== Test Complete ==="
echo ""
echo "Next steps:"
echo "1. If no temporary workers exist, first scale up (deploy CPU stress workload)"
echo "2. Then invoke Lambda to test scale-down"
echo "3. Monitor Lambda logs: aws logs tail /aws/lambda/k3s-autoscaler-function-64ea403 --follow"
