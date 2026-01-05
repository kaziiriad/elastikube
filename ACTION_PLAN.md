# K3s Autoscaler - Production Implementation Action Plan

## Progress Summary

| Phase | Status | Completion |
|-------|--------|------------|
| Phase 1: Infrastructure Foundation | ✅ COMPLETE | 100% |
| Phase 2: Lambda Autoscaler Controller | ⚠️ CODE COMPLETE | 95% (Testing Pending) |
| Phase 3: K3s Cluster Deployment | ✅ COMPLETE | 100% |
| Phase 4: Testing | ❌ NOT STARTED | 0% |
| Phase 5: Deployment & CI/CD | ⚠️ PARTIAL | 30% |
| Phase 6: Documentation | ⚠️ PARTIAL | 50% |

**Overall Progress: ~60% Complete**

### Completed Components
- ✅ AWS Infrastructure (VPC, EC2, DynamoDB, Lambda, S3, EventBridge)
- ✅ K3s Cluster (1 master + 2 workers, Ready)
- ✅ Prometheus (NodePort 30900)
- ✅ Lambda Autoscaler Code (all modules implemented)
- ✅ Ansible Deployment Playbooks
- ✅ Architecture Documentation

### Next Priority Tasks
1. Deploy Lambda function to AWS
2. Implement EC2 user data bootstrap script
3. Add CloudWatch alarms
4. Write integration tests
5. Create deployment CI/CD pipeline

---

## Overview

Build a production-grade autoscaling system for K3s clusters on AWS using:
- **Pulumi (Python)** for Infrastructure as Code
- **AWS Lambda** (Python 3.11) for autoscaling decisions
- **DynamoDB** for state management and distributed locking
- **CloudWatch** for monitoring and alerting

---

## Phase 1: Infrastructure Foundation (Pulumi) ✅ COMPLETE

### 1.1 Core AWS Resources
- [x] Create Pulumi project structure (Python)
- [x] Set up virtual environment and dependencies
- [x] Configure AWS provider and region
- [x] Create VPC with public/private subnets (Multi-AZ)
- [x] Create Internet Gateway and NAT Gateways
- [x] Create Security Groups:
  - K3s Master SG (allow 6443, 9090, 30900)
  - K3s Workers SG (allow from master)
  - Lambda SG (allow outbound to Prometheus)

### 1.2 K3s Master Infrastructure
- [x] Create EC2 instance (t3.small) for K3s master
- [x] Create IAM role for master node
- [x] Create Security Group for master
- [x] Deploy Prometheus on master (NodePort: 30900)

### 1.3 State & Storage Resources
- [x] Create S3 bucket for K3s token and scripts
- [x] Create DynamoDB table: `k3s-cluster-state`
  - Partition key: `cluster_id` (String)
  - TTL attribute: `ttl`
  - Enable Point-in-Time Recovery
- [x] Create DynamoDB table: `k3s-scaling-wal`
  - Partition key: `operation_id` (String)
  - GSI: `IncompleteOperations` (state, started_at)
  - TTL attribute: `ttl`

### 1.4 Lambda Function Infrastructure
- [x] Create IAM role for Lambda execution
  - EC2 permissions (RunInstances, TerminateInstances, DescribeInstances)
  - DynamoDB permissions (GetItem, PutItem, UpdateItem, Query)
  - S3 permissions (GetObject)
  - CloudWatch Logs permissions
- [x] Create EventBridge rule (cron: */2 * * * ? *)

### 1.5 CloudWatch Monitoring
- [x] Create Log Group: `/aws/lambda/k3s-autoscaler`
- [ ] Create CloudWatch Dashboard: `K3s-Autoscaler-Metrics`
- [ ] Create CloudWatch Alarms:
  - `HighClusterCPU` (> 85% for 10min)
  - `ScalingFailure` (> 3 errors in 10min)
  - `NodeProvisioningTimeout` (> 10min)
  - `DynamoDBLockTimeout` (> 5min)

---

## Phase 2: Lambda Autoscaler Controller ⚠️ CODE COMPLETE (Testing Pending)

### 2.1 Core Lambda Handler
- [x] Create `lambda_function.py` entry point
- [x] Implement environment variable loading
- [x] Initialize AWS clients (boto3)
- [x] Add structured logging with correlation IDs
- [x] Implement error handling and retry logic

### 2.2 Prometheus Metrics Collector (`lambda/src/metrics/`)
- [x] Create `prometheus_client.py`
- [x] Implement CPU usage query
- [x] Implement memory usage query
- [x] Implement pending pods query
- [x] Implement node count query
- [x] Add query timeout handling
- [ ] Add Prometheus authentication (if needed)

### 2.3 Scaling Decision Engine (`lambda/src/scaler/`)
- [x] Create `decision.py`
- [x] Implement scale-up logic:
  - CPU > 70% for 3 checks OR
  - Pending pods > 0 OR
  - Memory > 80%
- [x] Implement scale-down logic:
  - CPU < 30% AND Memory < 50% AND
  - No pending pods AND
  - Above min nodes
- [x] Implement cooldown tracking
- [x] Add pressure calculation algorithm
- [x] Add nodes-needed calculation
- [x] Add min/max boundary checks

### 2.4 DynamoDB State Manager (`lambda/src/state/`)
- [x] Create `dynamodb_manager.py`
- [x] Implement cluster state CRUD operations
- [x] Implement distributed lock:
  - Acquire lock with conditional write
  - Lock TTL (120 seconds)
  - Release lock with ownership check
- [x] Implement Write-Ahead Log (WAL):
  - Log operations before execution
  - Mark operations complete/failed
  - Recover incomplete operations
- [x] Implement worker nodes tracking

### 2.5 EC2 Provisioner (`lambda/src/scaler/`)
- [x] Create `ec2_provisioner.py`
- [x] Implement EC2 instance launch:
  - Use pre-baked AMI with K3s agent
  - Tag instances with cluster and node name
  - Distribute across AZs
  - Use ClientToken for idempotency
- [x] Implement EC2 instance termination:
  - Verify instance exists
  - Graceful termination
  - Wait for termination confirmation
- [x] Add timeout handling

### 2.6 Node Drainer (`lambda/src/scaler/`)
- [x] Create `k8s_drainer.py`
- [x] Implement kubectl drain via k8s Python client
- [x] Add safety checks:
  - Skip kube-system pods
  - Skip DaemonSet pods
  - Skip pods with local storage
  - Check PodDisruptionBudgets
- [x] Implement cordon/uncordon
- [x] Add pod eviction timeout (5 minutes)

### 2.7 Utilities (`lambda/src/utils/`)
- [x] Create `logger.py` - Structured CloudWatch logging
- [x] Create `lock.py` - Distributed lock wrapper
- [x] Create `wal.py` - Write-Ahead Log operations
- [x] Create `config.py` - Configuration validation

---

## Phase 3: K3s Cluster Deployment ✅ COMPLETE

### 3.1 K3s Cluster Setup (Ansible)
- [x] Create Ansible playbook structure
- [x] Create common role (dependencies, kernel modules, sysctl)
- [x] Create k3s-master role (install server, get token)
- [x] Create k3s-worker role (install agent, join cluster)
- [x] Deploy Prometheus with Helm (NodePort: 30900)
- [x] Verify cluster health and node readiness

### 3.2 Worker Bootstrap (Future)
- [ ] Create user data script for EC2 instances
- [ ] Fetch K3s token from S3
- [ ] Install K3s agent
- [ ] Configure node labels (AZ, instance type)
- [ ] Install node-exporter for Prometheus
- [ ] Signal readiness to DynamoDB
- [ ] Add error handling and logging

### 3.3 AMI Creation (Future)
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

## Phase 6: Documentation & Operations ⚠️ IN PROGRESS

### 6.1 Documentation
- [x] Create architecture documentation (`docs/AUTOSCALER_ARCHITECTURE.md`)
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
