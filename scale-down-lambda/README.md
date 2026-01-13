# Worker Cleanup Lambda

A Lambda function for managing K3s worker node cleanup operations including draining and scaling down.

## Purpose

This Lambda handles worker node lifecycle operations:
- **List workers**: View all worker instances with their status
- **Drain workers**: Safely evict pods before removal (kubectl drain via SSM)
- **Terminate workers**: Remove worker instances from the cluster
- **Scale down**: Automatically select and remove a non-permanent worker (LIFO strategy)

## Usage

### Build the Lambda

```bash
cd /mnt/e/custom_autoscaler/production/worker-cleanup-lambda
./build.sh
```

### Deploy with Pulumi

Update the Pulumi infrastructure to include this Lambda function (see `infrastructure/pulumi/__main__.py`).

### Invoke the Lambda

**List all workers:**
```bash
aws lambda invoke --function-name worker-cleanup-lambda \
  --payload '{"action": "list"}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

**Describe a specific worker:**
```bash
aws lambda invoke --function-name worker-cleanup-lambda \
  --payload '{"action": "describe", "instance_id": "i-xxxxxxxxx"}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

**Drain a worker (evict pods, mark unschedulable):**
```bash
aws lambda invoke --function-name worker-cleanup-lambda \
  --payload '{"action": "drain", "instance_id": "i-xxxxxxxxx"}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

**Terminate a worker:**
```bash
aws lambda invoke --function-name worker-cleanup-lambda \
  --payload '{"action": "terminate", "instance_id": "i-xxxxxxxxx"}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

**Scale down (select non-permanent worker, drain + terminate):**
```bash
aws lambda invoke --function-name worker-cleanup-lambda \
  --payload '{"action": "scale_down"}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CLUSTER_NAME` | Name of the K3s cluster | `production-k3s` |
| `SECURITY_GROUP_ID` | Security group ID for filtering | (from Pulumi) |
| `SUBNET_ID` | Subnet ID for filtering | (from Pulumi) |

## Scale Down Strategy

The `scale_down` action uses **LIFO (Last In, First Out)** strategy:

1. **Excludes permanent workers**: Workers with tag `Permanent=true` are never selected
2. **Prefers autoscaler-created workers**: Selects instances with `CreatedBy=autoscaler` tag first
3. **Selects most recently launched**: Sorts by `LaunchTime` descending
4. **Safe removal**: Executes `kubectl drain` before termination

## Response Format

### List Response
```json
{
  "worker_count": 3,
  "workers": [
    {
      "instance_id": "i-xxxxxxxxx",
      "state": "running",
      "private_ip": "10.0.2.11",
      "tags": {...},
      "is_permanent": true,
      "created_by": "ansible"
    }
  ]
}
```

### Drain Response
```json
{
  "message": "Node ip-10-0-2-xxx drained successfully",
  "instance_id": "i-xxxxxxxxx",
  "node_name": "ip-10-0-2-xxx",
  "status": "Success"
}
```

### Scale Down Response
```json
{
  "message": "Scale-down completed",
  "instance_id": "i-xxxxxxxxx",
  "drain_result": {...},
  "terminate_result": {...}
}
```

## IAM Permissions Required

The Lambda needs the following permissions:
- `ec2:DescribeInstances` - Query worker instances
- `ec2:DescribeTags` - Check instance tags
- `ec2:TerminateInstances` - Terminate instances
- `ssm:GetParameter` - Fetch master IP from SSM
- `ssm:SendCommand` - Execute kubectl drain via SSM
- `ssm:GetCommandInvocation` - Get command results
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` - CloudWatch logging

## How Kubectl Drain Works via SSM

1. Lambda fetches master IP from SSM Parameter Store
2. Lambda sends SSM RunShellScript command to master
3. Master executes `kubectl drain` on the target node
4. Lambda polls for command completion
5. If drain succeeds, proceeds with termination

The drain command used:
```bash
sudo kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --timeout=120s
```

## Integration with Autoscaler

This Lambda can be invoked by the main autoscaler Lambda for scale-down operations, or used independently for manual worker management.

Example from autoscaler:
```python
lambda_client.invoke(
    FunctionName="worker-cleanup-lambda",
    Payload=json.dumps({"action": "scale_down"}),
    InvocationType="RequestResponse"
)
```
