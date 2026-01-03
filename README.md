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
- Terraform >= 1.0
- Python 3.11+
- Existing K3s cluster

### Deploy Infrastructure

```bash
cd infrastructure/terraform
terraform init
terraform plan
terraform apply
```

### Deploy Lambda Function

```bash
cd lambda
pip install -r requirements.txt -t package
cd package && zip -r ../lambda_function.zip .
cd .. && zip -g lambda_function.zip lambda_function.py
aws lambda update-function-code --function-name k3s-autoscaler --zip-file fileb://lambda_function.zip
```

## Directory Structure

```
production/
├── .github/workflows/          # GitHub Actions CI/CD
├── ci/                          # CI/CD configurations
├── infrastructure/
│   ├── terraform/              # Terraform IaC
│   └── cloudformation/         # CloudFormation templates
├── lambda/
│   ├── src/
│   │   ├── metrics/            # Prometheus metrics collector
│   │   ├── scaler/             # Scaling decision engine
│   │   ├── state/              # DynamoDB state management
│   │   └── utils/              # Utilities (logging, locks, etc.)
│   └── tests/                  # Lambda function tests
├── monitoring/
│   ├── alarms/                 # CloudWatch alarms
│   └── dashboards/             # CloudWatch dashboards
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
