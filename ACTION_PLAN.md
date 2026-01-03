# K3s Autoscaler - Production Implementation Action Plan

## Overview

Build a production-grade autoscaling system for K3s clusters on AWS using:
- **Pulumi (Python)** for Infrastructure as Code
- **AWS Lambda** (Python 3.11) for autoscaling decisions
- **DynamoDB** for state management and distributed locking
- **CloudWatch** for monitoring and alerting

---

## Phase 1: Infrastructure Foundation (Pulumi)

### 1.1 Core AWS Resources
- [ ] Create Pulumi project structure (Python)
- [ ] Set up virtual environment and dependencies
- [ ] Configure AWS provider and region
- [ ] Create VPC with public/private subnets (Multi-AZ)
- [ ] Create Internet Gateway and NAT Gateways
- [ ] Create Security Groups:
  - K3s Master SG (allow 6443, 9090, 30900)
  - K3s Workers SG (allow from master)
  - Lambda SG (allow outbound to Prometheus)

### 1.2 K3s Master Infrastructure
- [ ] Create EC2 instance (t3.medium) for K3s master
- [ ] Create IAM role for master node
- [ ] Create Security Group for master
- [ ] Generate and store K3s cluster token in SSM Parameter Store
- [ ] Deploy Prometheus on master (NodePort: 30900)

### 1.3 State & Storage Resources
- [ ] Create S3 bucket for K3s token and scripts
- [ ] Create DynamoDB table: `k3s-cluster-state`
  - Partition key: `cluster_id` (String)
  - TTL attribute: `ttl`
  - Enable Point-in-Time Recovery
- [ ] Create DynamoDB table: `k3s-scaling-wal`
  - Partition key: `operation_id` (String)
  - GSI: `IncompleteOperations` (state, started_at)
  - TTL attribute: `ttl`

### 1.4 Lambda Function Infrastructure
- [ ] Create IAM role for Lambda execution
  - EC2 permissions (RunInstances, TerminateInstances, DescribeInstances)
  - DynamoDB permissions (GetItem, PutItem, UpdateItem, Query)
  - S3 permissions (GetObject)
  - SSM permissions (GetParameter)
  - CloudWatch Logs permissions
- [ ] Create EventBridge rule (cron: */2 * * * ? *)
- [ ] Create Lambda function placeholder

### 1.5 CloudWatch Monitoring
- [ ] Create Log Group: `/aws/lambda/k3s-autoscaler`
- [ ] Create CloudWatch Dashboard: `K3s-Autoscaler-Metrics`
- [ ] Create CloudWatch Alarms:
  - `HighClusterCPU` (> 85% for 10min)
  - `ScalingFailure` (> 3 errors in 10min)
  - `NodeProvisioningTimeout` (> 10min)
  - `DynamoDBLockTimeout` (> 5min)

---

## Phase 2: Lambda Autoscaler Controller

### 2.1 Core Lambda Handler
- [ ] Create `lambda_function.py` entry point
- [ ] Implement environment variable loading
- [ ] Initialize AWS clients (boto3)
- [ ] Add structured logging with correlation IDs
- [ ] Implement error handling and retry logic

### 2.2 Prometheus Metrics Collector (`lambda/src/metrics/`)
- [ ] Create `prometheus_client.py`
- [ ] Implement CPU usage query
- [ ] Implement memory usage query
- [ ] Implement pending pods query
- [ ] Implement node count query
- [ ] Add query timeout handling
- [ ] Add Prometheus authentication (if needed)

### 2.3 Scaling Decision Engine (`lambda/src/scaler/`)
- [ ] Create `decision.py`
- [ ] Implement scale-up logic:
  - CPU > 70% for 3 checks OR
  - Pending pods > 0 OR
  - Memory > 80%
- [ ] Implement scale-down logic:
  - CPU < 30% AND Memory < 50% AND
  - No pending pods AND
  - Above min nodes
- [ ] Implement cooldown tracking
- [ ] Add pressure calculation algorithm
- [ ] Add nodes-needed calculation
- [ ] Add min/max boundary checks

### 2.4 DynamoDB State Manager (`lambda/src/state/`)
- [ ] Create `dynamodb_manager.py`
- [ ] Implement cluster state CRUD operations
- [ ] Implement distributed lock:
  - Acquire lock with conditional write
  - Lock TTL (120 seconds)
  - Release lock with ownership check
- [ ] Implement Write-Ahead Log (WAL):
  - Log operations before execution
  - Mark operations complete/failed
  - Recover incomplete operations
- [ ] Implement worker nodes tracking

### 2.5 EC2 Provisioner (`lambda/src/scaler/`)
- [ ] Create `ec2_provisioner.py`
- [ ] Implement EC2 instance launch:
  - Use pre-baked AMI with K3s agent
  - Tag instances with cluster and node name
  - Distribute across AZs
  - Use ClientToken for idempotency
- [ ] Implement EC2 instance termination:
  - Verify instance exists
  - Graceful termination
  - Wait for termination confirmation
- [ ] Add timeout handling

### 2.6 Node Drainer (`lambda/src/scaler/`)
- [ ] Create `k8s_drainer.py`
- [ ] Implement kubectl drain via k8s Python client
- [ ] Add safety checks:
  - Skip kube-system pods
  - Skip DaemonSet pods
  - Skip pods with local storage
  - Check PodDisruptionBudgets
- [ ] Implement cordon/uncordon
- [ ] Add pod eviction timeout (5 minutes)

### 2.7 Utilities (`lambda/src/utils/`)
- [ ] Create `logger.py` - Structured CloudWatch logging
- [ ] Create `lock.py` - Distributed lock wrapper
- [ ] Create `wal.py` - Write-Ahead Log operations
- [ ] Create `config.py` - Configuration validation

---

## Phase 3: EC2 User Data Scripts

### 3.1 Worker Bootstrap Script
- [ ] Create user data script for EC2 instances
- [ ] Fetch K3s token from S3
- [ ] Install K3s agent
- [ ] Configure node labels (AZ, instance type)
- [ ] Install node-exporter for Prometheus
- [ ] Signal readiness to DynamoDB
- [ ] Add error handling and logging

### 3.2 AMI Creation
- [ ] Create Packer template (optional)
- [ ] Build AMI with K3s agent pre-installed
- [ ] Test AMI boot process
- [ ] Store AMI ID in SSM Parameter Store

---

## Phase 4: Testing

### 4.1 Unit Tests
- [ ] Test scaling decision logic
- [ ] Test pressure calculation
- [ ] Test cooldown logic
- [ ] Test DynamoDB lock acquisition/release
- [ ] Test WAL operations
- [ ] Test idempotent EC2 operations

### 4.2 Integration Tests
- [ ] Test end-to-end scale-up flow
- [ ] Test end-to-end scale-down flow
- [ ] Test DynamoDB state persistence
- [ ] Test Lambda execution within timeout
- [ ] Test CloudWatch metrics emission

### 4.3 Failure Scenarios
- [ ] Test Lambda timeout recovery
- [ ] Test DynamoDB lock expiration
- [ ] Test EC2 quota exceeded
- [ ] Test Prometheus unavailable
- [ ] Test concurrent Lambda executions

---

## Phase 5: Deployment & CI/CD

### 5.1 GitHub Actions
- [ ] Create workflow for Pulumi preview
- [ ] Create workflow for Pulumi deploy (manual approval)
- [ ] Create workflow for Lambda deployment
- [ ] Add automated tests on PR

### 5.2 Deployment Scripts
- [ ] `scripts/deploy-infrastructure.sh` - Pulumi up
- [ ] `scripts/deploy-lambda.sh` - Package and deploy Lambda
- [ ] `scripts/bootstrap-cluster.sh` - Initial cluster setup
- [ ] `scripts/destroy.sh` - Clean up resources

---

## Phase 6: Documentation & Operations

### 6.1 Documentation
- [ ] Update README with deployment instructions
- [ ] Create troubleshooting guide
- [ ] Document scaling thresholds and behavior
- [ ] Create runbook for common operations

### 6.2 Operations
- [ ] Create CloudWatch dashboard
- [ ] Set up SNS notifications for alarms
- [ ] Create incident response procedures
- [ ] Document rollback procedures

---

## Dependencies

| Category | Tool/Service | Version |
|----------|--------------|---------|
| IaC | Pulumi (Python) | Latest |
| Runtime | Python | 3.11 |
| Lambda | boto3 | Latest |
| Database | DynamoDB | - |
| Monitoring | CloudWatch | - |
| Container | K3s | Latest |
| Testing | pytest | Latest |

---

## Success Criteria

- [ ] Lambda executes within 60-second timeout
- [ ] Scale-up completes within 5 minutes
- [ ] Scale-down completes within 10 minutes
- [ ] No race conditions during concurrent operations
- [ ] Automatic recovery from Lambda failures
- [ ] CloudWatch alarms trigger correctly
- [ ] Pulumi deployment succeeds

---

## Notes

- **State Management**: DynamoDB is the source of truth, not EC2 or Kubernetes
- **Lock TTL**: 120 seconds prevents deadlocks
- **Cooldown**: 5 minutes (scale-up), 15 minutes (scale-down)
- **Max Scale**: 3 nodes at once (scale-up), 1 node (scale-down)
- **Node Limits**: Min 2, Max 10
