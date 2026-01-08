# K3s Autoscaler - Production

Production-grade autoscaling system for K3s clusters on AWS using AWS Lambda, DynamoDB, and EC2.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         AWS Cloud                                │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                      VPC (Multi-AZ)                         │ │
│  │                                                              │ │
│  │  ┌──────────────────┐      ┌──────────────────┐            │ │
│  │  │   K3s Master     │      │  Worker Nodes    │            │ │
│  │  │   (t3.medium)    │◄────►│  (t3.small)      │            │ │
│  │  │                  │      │  Auto-scaled     │            │ │
│  │  │  ┌──────────┐    │      │  (2-10 nodes)    │            │ │
│  │  │  │Prometheus│    │      └──────────────────┘            │ │
│  │  │  └──────────┘    │                                       │ │
│  │  └──────────────────┘                                       │ │
│  │           ▲                                                  │ │
│  └───────────┼──────────────────────────────────────────────────┘ │
│              │ HTTP API                                           │
│              │                                                    │
│  ┌───────────▼──────────────────────────────────────────────┐   │
│  │              AWS Lambda (Python 3.11)                     │   │
│  │         Autoscaler Decision Engine                        │   │
│  │  ┌──────────────────────────────────────────────────┐    │   │
│  │  │ 1. Query Prometheus metrics                      │    │   │
│  │  │ 2. Make scaling decision                         │    │   │
│  │  │ 3. Acquire DynamoDB lock                         │    │   │
│  │  │ 4. Launch/Terminate EC2 instances                │    │   │
│  │  │ 5. Update cluster state                          │    │   │
│  │  └──────────────────────────────────────────────────┘    │   │
│  └────────────────────────────────────────────────────────────┘  │
│              │                │              │                    │
│  ┌───────────▼────┐  ┌────────▼────┐  ┌─────▼──────┐            │
│  │   DynamoDB     │  │     S3      │  │ CloudWatch │            │
│  │  Cluster State │  │  K3s Token  │  │    Logs    │            │
│  │  & Locks       │  │  & Scripts  │  │  & Alarms  │            │
│  └────────────────┘  └─────────────┘  └────────────┘            │
│              ▲                                                    │
│  ┌───────────┴──────────┐                                        │
│  │  EventBridge Rule    │                                        │
│  │  (Trigger: 2 min)    │                                        │
│  └──────────────────────┘                                        │
└───────────────────────────────────────────────────────────────────┘
```

## Key Components

| Component | Purpose | Technology |
|-----------|---------|------------|
| **Autoscaler Controller** | Makes scaling decisions | AWS Lambda (Python 3.11) |
| **State Management** | Distributed locking & state | DynamoDB |
| **Metrics Collection** | Cluster metrics | Prometheus |
| **Node Provisioning** | EC2 instance management | AWS EC2 API |
| **Token Storage** | K3s join token | AWS S3 |
| **Monitoring** | Logs, metrics, alarms | CloudWatch |
| **Infrastructure** | IaC deployment | Terraform / CloudFormation |

## Scaling Logic

**Scale UP when:**
- Average CPU > 70% for 3 consecutive checks (6 minutes)
- OR Pending pods exist for > 3 minutes
- OR Memory > 80% for 3 consecutive checks

**Scale DOWN when:**
- Average CPU < 30% for 10 minutes
- AND Memory < 50% for 10 minutes
- AND No pending pods for 10 minutes
- AND Current nodes > minimum (2)

## Quick Start

### Prerequisites

- AWS CLI configured
- Pulumi >= 3.0
- Ansible >= 2.15
- Python 3.11+

### Deploy Infrastructure

```bash
cd infrastructure/pulumi
pulumi stack init prod
pulumi up
```

### Deploy K3s with Ansible

```bash
cd infrastructure/scripts
./deploy-k3s.sh
```

## Deployment

### Manual Deployment Steps

1. **Push to Remote Origin**
   ```bash
   git push origin master
   ```

2. **Deploy Infrastructure (Pulumi)**
   ```bash
   cd infrastructure/pulumi
   pulumi up
   ```

3. **Deploy K3s Cluster (Ansible)**
   ```bash
   cd infrastructure/ansible
   ansible-playbook -i inventory/hosts.ini site.yml
   ```

4. **Deploy Lambda Function**
   ```bash
   cd lambda
   ./build.sh
   pulumi up  # From pulumi directory
   ```

> **Note:** GitHub Actions workflows are disabled. Use manual deployment for infrastructure and application changes.

## Directory Structure

```
production/
├── .github/workflows.disabled/  # Disabled GitHub Actions workflows
├── infrastructure/
│   ├── pulumi/                 # Pulumi IaC for AWS resources
│   ├── ansible/                # Ansible playbooks for K3s setup
│   ├── scripts/                # Deployment scripts
│   └── cloudformation/         # CloudFormation templates
├── lambda/
│   ├── src/
│   │   ├── metrics/            # Prometheus metrics collector
│   │   ├── scaler/             # Scaling decision engine
│   │   ├── state/              # DynamoDB state management
│   │   └── utils/              # Utilities (logging, locks, etc.)
│   ├── tests/                  # Lambda function tests
│   └── build.sh                # Lambda deployment package builder
├── monitoring/
│   ├── alarms/                 # CloudWatch alarms
│   └── dashboards/             # CloudWatch dashboards
├── docs/                       # Documentation
└── scripts/                    # Deployment and utility scripts
```

## Configuration

Environment variables for Lambda:

```bash
PROMETHEUS_URL=http://<master-ip>:30900
K3S_MASTER_IP=<master-private-ip>
DYNAMODB_TABLE=k3s-cluster-state
S3_BUCKET=k3s-cluster-config
MIN_NODES=2
MAX_NODES=10
SCALE_UP_THRESHOLD_CPU=70
SCALE_DOWN_THRESHOLD_CPU=30
SCALE_UP_COOLDOWN=300
SCALE_DOWN_COOLDOWN=900
```

## Monitoring

- **CloudWatch Logs**: `/aws/lambda/k3s-autoscaler`
- **CloudWatch Dashboard**: `K3s-Autoscaler-Metrics`
- **Custom Metrics**: `K3sAutoscaler` namespace

## License

MIT
