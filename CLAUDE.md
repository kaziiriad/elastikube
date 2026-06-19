# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ElastiKube** is a production-grade autoscaling system for K3s clusters on AWS. It uses an event-driven Lambda architecture with DynamoDB state management to automatically scale worker nodes based on CPU, memory, and pod scheduling pressure.

## Architecture

```
EventBridge (2-min schedule) → Decision Lambda → ScaleUp/ScaleDown EventBridge events
                                       ↓
                        DynamoDB (state, WAL, locks)
                                       ↓
                    Scale-Up/Scale-Down/Cleanup Lambdas
                                       ↓
                              EC2 API (launch/terminate)
```

### Core Lambdas

| Lambda | Purpose | Entry Point |
|--------|---------|-------------|
| `decision-lambda` | Fetches metrics, makes scaling decisions | `decision-lambda/main.py` |
| `scale-up-lambda` | Launches EC2 worker instances | `scale-up-lambda/main.py` |
| `scale-down-lambda` | Drains and terminates workers | `scale-down-lambda/main.py` |
| `cleanup-lambda` | Handles stale nodes and spot interruptions | `cleanup-lambda/main.py` |

### State Management (DynamoDB)

| Table | Purpose | Key |
|-------|---------|-----|
| `k3s-cluster-state` | Cluster state, scaling status, locks | Hash: `cluster_id` |
| `k3s-scaling-wal` | Write-Ahead Log for crash recovery | Hash: `operation_id` |

### Infrastructure as Code

Pulumi (Python) in `infrastructure/pulumi/__main__.py`. Creates VPC, EC2 instances, Lambda functions, DynamoDB tables, EventBridge rules, IAM roles, and CloudWatch alarms.

## Common Commands

### Building Lambdas

```bash
# Decision Lambda
cd production/decision-lambda && ./build.sh

# Scale-Up Lambda
cd production/scale-up-lambda && ./build.sh

# Scale-Down Lambda
cd production/scale-down-lambda && ./build.sh

# Cleanup Lambda
cd production/cleanup-lambda && ./build.sh
```

Each `build.sh` creates a `build/lambda.zip` deployment package using `uv` for dependency management.

### Infrastructure Deployment

```bash
cd production/infrastructure/pulumi

# Install dependencies
uv sync

# Preview changes
pulumi preview

# Deploy
pulumi up

# Destroy
pulumi destroy
```

### Running Decision Lambda Tests

```bash
cd production/decision-lambda
uv sync --frozen --no-dev
pytest tests/
```

### ML Training

```bash
cd production/ml_training
uv sync
# Notebooks in notebooks/
# Models in models/, data in data/, validation in validation/
```

## Key Design Patterns

### LIFO Scale-Down
Workers tagged `Permanent=true` are excluded. Among others, most recently launched is selected first. Prevents scale-down during high load by requiring CPU AND memory both low.

### Distributed Locking
DynamoDB conditional writes (optimistic locking) prevent concurrent Lambda executions. 10-second timeout with 1-second retries.

### Crash Recovery (WAL)
Write-Ahead Log tracks incomplete operations. Operations older than 10 minutes marked FAILED. Recent incomplete operations block new scaling.

### Time-Aware Scaling
| Period | Hours | Scale-Up | Scale-Down |
|--------|-------|----------|------------|
| Peak | 9 AM - 9 PM | 85% | 60% |
| Off-Peak | 9 PM - 9 AM | 60% | 40% |

### Flash Sale Detection
CPU spike >30% in 2 minutes triggers immediate scale-up, bypassing cooldowns.

## Key Files

| Path | Purpose |
|------|---------|
| `decision-lambda/src/scaler/scaling.py` | Core scaling logic and threshold evaluation |
| `decision-lambda/src/state/manager.py` | DynamoDB state operations |
| `scale-up-lambda/main.py` | EC2 instance launch with spot fallback |
| `scale-down-lambda/main.py` | LIFO node selection and kubectl drain |
| `infrastructure/pulumi/__main__.py` | AWS infrastructure definition |
| `docs/iam-policies.json` | Complete IAM permissions |

## Environment Variables (Lambdas)

| Variable | Description |
|----------|-------------|
| `CLUSTER_NAME` | K3s cluster name (default: `production-k3s`) |
| `USE_SPOT_INSTANCES` | Enable spot instance with on-demand fallback |
| `K3S_MASTER_IP_SSM_PARAM` | SSM Parameter Store path for master IP |
| `K3S_JOIN_TOKEN_SECRETS_ID` | Secrets Manager ID for join token |
| `USER_DATA_S3_BUCKET` | S3 bucket for bootstrap scripts |
| `DYNAMODB_STATE_TABLE` | Cluster state table name |
| `DYNAMODB_WAL_TABLE` | WAL table name |
| `MIN_NODES` / `MAX_NODES` | Scaling boundaries |
| `SCALE_UP_COOLDOWN` / `SCALE_DOWN_COOLDOWN` | Cooldown periods (seconds) |

## Multi-AZ Architecture

- 3 private subnets across `ap-southeast-1a/b/c`
- Scale-up uses **round-robin** across AZs (tracked via `last_subnet_index` in DynamoDB)
- Scale-down uses **LIFO** (naturally balances distribution)
- Master and permanent workers in AZ-a only