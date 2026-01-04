# K3s Autoscaler Infrastructure

Pulumi (Python) infrastructure for the K3s autoscaler.

## Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager
- AWS CLI configured
- Pulumi CLI installed

```bash
# Install uv
pip install uv

# Install Pulumi
curl -fsSL https://get.pulumi.com | sh

# Or via homebrew
brew install pulumi
```

## Getting Started

### 1. Install Dependencies

```bash
cd infrastructure/pulumi
uv sync
```

### 2. Configure AWS Region

Default is `ap-southeast-1` (Singapore). To change:

```bash
pulumi config set aws:region us-east-1
```

### 3. (Optional) Configure AMI ID

The default AMI is for Ubuntu 22.04 in ap-southeast-1. Find your region's AMI:

```bash
# Ubuntu 22.04 AMIs: https://cloud-images.ubuntu.com/locator/ec2/
pulumi config set ec2:amiId ami-xxxxx
```

### 4. Preview Changes

```bash
pulumi preview
```

### 5. Deploy

```bash
pulumi up
```

## Configuration

All configuration is in `Pulumi.yaml`. Override with `pulumi config set`:

| Key | Default | Description |
|-----|---------|-------------|
| `k3s:clusterName` | `production-k3s` | K3s cluster name |
| `k3s:minNodes` | `2` | Minimum worker nodes |
| `k3s:maxNodes` | `10` | Maximum worker nodes |
| `ec2:masterInstanceType` | `t3.medium` | Master instance type |
| `ec2:workerInstanceType` | `t3.small` | Worker instance type |
| `autoscaler:checkInterval` | `120` | Autoscaler check interval (seconds) |
| `autoscaler:scaleUpThreshold` | `70` | CPU % to scale up |
| `autoscaler:scaleDownThreshold` | `30` | CPU % to scale down |
| `autoscaler:scaleUpCooldown` | `300` | Scale-up cooldown (seconds) |
| `autoscaler:scaleDownCooldown` | `900` | Scale-down cooldown (seconds) |

## Resources Created

### State & Storage
- **S3 Bucket**: K3s token and scripts storage
- **DynamoDB Table (k3s-cluster-state)**: Cluster state with TTL
- **DynamoDB Table (k3s-scaling-wal)**: Write-Ahead Log for operations

### IAM
- **Lambda Role**: k3s-autoscaler-lambda-role
  - EC2 permissions (RunInstances, TerminateInstances, DescribeInstances)
  - DynamoDB permissions
  - S3 permissions
  - SSM permissions
  - CloudWatch permissions
- **Worker Role**: k3s-worker-node-role
  - S3 access for K3s token
  - DynamoDB access for state updates
  - SSM for Session Manager
- **Instance Profile**: k3s-worker-instance-profile

### Monitoring
- **CloudWatch Log Group**: /aws/lambda/k3s-autoscaler
- **CloudWatch Alarms**:
  - High CPU (> 85% for 10min)
  - Scaling Failure (> 3 errors in 10min)
  - Provisioning Timeout (> 10min)
  - Lock Timeout (> 5min)

### Event-Driven
- **EventBridge Rule**: Triggers every 2 minutes

## Outputs

```bash
pulumi stack output
```

| Output | Description |
|--------|-------------|
| `cluster_name` | K3s cluster name |
| `dynamodb_cluster_state_table` | Cluster state table name |
| `dynamodb_wal_table` | WAL table name |
| `s3_config_bucket` | S3 bucket name |
| `lambda_role_arn` | Lambda IAM role ARN |
| `worker_instance_profile` | EC2 instance profile for workers |
| `cloudwatch_log_group` | CloudWatch log group name |
| `event_rule_arn` | EventBridge rule ARN |

## Destroy

```bash
pulumi destroy
```

## Next Steps

1. **Deploy Lambda function**: Package and deploy from `lambda/` directory
2. **Configure existing cluster**: Update `K3S_MASTER_IP` environment variable
3. **Generate K3s token**: Store in SSM Parameter Store and S3 bucket
4. **Create EventBridge target**: Connect rule to Lambda function
