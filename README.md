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

## CI/CD with GitHub Actions

This project uses GitHub Actions with self-hosted runners on the bastion host.

### Required GitHub Secrets

Configure the following secrets in your GitHub repository settings:

| Secret Name | Description | Example |
|-------------|-------------|---------|
| `AWS_ACCESS_KEY_ID` | AWS access key with EC2/VPC/IAM permissions | `AKIAIOSFODNN7EXAMPLE` |
| `AWS_SECRET_ACCESS_KEY` | AWS secret access key | `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY` |
| `PULUMI_ACCESS_TOKEN` | Pulumi authentication token | `pulum-abc123...` |
| `SSH_PRIVATE_KEY` | SSH key pair for EC2 instances (contents of .pem file) | `-----BEGIN RSA PRIVATE KEY-----\n...` |
| `GIT_RUNNER_TOKEN` | GitHub Actions runner registration token | `ABCD1234...` (from repo Settings → Actions → Runners → New self-hosted runner) |

### Workflow Triggers

- **Deploy Infrastructure** (`infra.yml`): Triggered on push to `master` with changes in `infrastructure/pulumi/**`
  - Runs on GitHub-hosted `ubuntu-latest` runner
  - Provisions VPC, bastion, and K3s EC2 instances

- **Setup Bastion Runner** (`setup-bastion-runner.yml`): Triggered after successful infrastructure deployment
  - Installs GitHub Actions self-hosted runner on bastion
  - Installs Ansible, kubectl, Pulumi, and other tools

- **Deploy K3s** (`k3s-deploy.yml`): Triggered on push to `master` with changes in `infrastructure/ansible/**`, or after runner setup
  - Runs on **self-hosted** bastion runner
  - Deploys K3s cluster with Ansible
  - Has direct access to private subnet K3s nodes

### Manual Deployment

For local deployment, see:
- Infrastructure: `infrastructure/pulumi/README.md`
- K3s setup: `infrastructure/scripts/deploy-k3s.sh`

## Directory Structure

```
production/
├── .github/workflows/          # GitHub Actions CI/CD
│   ├── infra.yml               # Infrastructure deployment (Pulumi)
│   ├── setup-bastion-runner.yml # GitHub Actions runner setup
│   └── k3s-deploy.yml          # K3s cluster deployment (Ansible)
├── ci/                          # CI/CD configurations
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
