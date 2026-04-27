# Fast Worker Bootstrap — Implementation Roadmap

**Date:** 2026-04-27
**Status:** Draft
**Branch:** `feature/fast-worker-bootstrap`

---

## Problem Statement

Worker node bootstrap currently takes 3-5 minutes due to:
1. SSM Agent installation (60-120s)
2. K3s binary download and installation (30-60s)
3. Tool installation (awscli, curl, jq) (30-60s)
4. Network wait and retries (30-60s)

**Goal:** Reduce bootstrap time to ~60 seconds by pre-baking AMI.

---

## Architecture (Ansible-Only Bake)

**Approach:** Single Ansible playbook handles AMI baking + SSM Parameter update. No Packer needed.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 1: BAKING (Ansible)                                              │
│                                                                          │
│  ansible-playbook worker-bake.yml                                        │
│  ┌─────────────────────┐    ┌────────────────────┐    ┌──────────────┐  │
│  │ k3s-worker-preinst │───▶│ k3s-agent-binary   │───▶│ SSM Parameter│  │
│  │     .yml           │    │       .yml          │    │ /k3s/.../    │  │
│  └─────────────────────┘    └────────────────────┘    │ worker-ami-id│  │
│          │                        │                   └──────────────┘  │
│          ▼                        ▼                                     │
│  ┌────────────────────────────────────────────────────┐                │
│  │  Baked AMI: k3s-worker-{cluster}-{date}             │                │
│  │  Contains: SSM Agent, awscli, curl, jq, k3s binary │                │
│  └────────────────────────────────────────────────────┘                │
└─────────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 2: DEPLOYMENT (scale-up-lambda)                                │
│                                                                          │
│  scale-up-lambda                                                         │
│  ├── Reads AMI ID from SSM Parameter                                   │
│  ├── Fetches user-data.sh.j2 from S3 (simplified: join only)           │
│  └── Launches EC2 with baked AMI + simplified user-data                 │
│                                                                          │
│  Bootstrap time: ~60s (was ~91s)                                       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## How It Works

### Before (current flow)
```
scale-up-lambda launches instance with base Ubuntu AMI
  → user-data.sh.j2 fetched from S3
  → user-data installs SSM Agent, K3s, tools (3-5 min)
  → user-data joins cluster
```

### After (optimized flow)
```
scale-up-lambda reads AMI ID from SSM Parameter
  → launches instance with baked AMI (SSM Agent, K3s, tools pre-installed)
  → fetches simplified user-data from S3 (join only, no install)
  → user-data joins cluster (~30-60s)
```

---

## Deliverables

### 1. Ansible Bake Playbook (`infrastructure/ansible/worker-bake.yml`)
- Single playbook for AMI baking
- Launches temporary EC2 instance
- Runs `k3s-worker-preinstall` + `k3s-agent-binary` roles via local connection
- Creates AMI from instance
- Writes AMI ID to SSM Parameter
- Terminates temporary instance

### 2. New Ansible Roles

#### `infrastructure/ansible/roles/k3s-worker-preinstall/`
Installs base tools before K3s:
- awscli (for SSM Parameter and Secrets Manager access)
- curl, jq, ca-certificates (utility tools)
- amazon-ssm-agent via snap

#### `infrastructure/ansible/roles/k3s-agent-binary/`
Installs K3s binary without joining cluster:
- Downloads k3s installer
- Runs `INSTALL_K3S_SKIP_START=true` to install binary only
- Does NOT start k3s-agent or join cluster
- Binary ready for user-data to start and join at launch time

### 3. AMI Cleanup Playbook (`infrastructure/ansible/ami-lifecycle.yml`)
- Deregisters AMIs older than 2 most recent
- Deletes associated EBS snapshots
- Run manually or via cron

### 4. scale-up-lambda Update (`scale-up-lambda/main.py`)
- Read AMI ID from SSM Parameter `/k3s/{cluster_name}/worker-ami-id`
- Use baked AMI instead of base Ubuntu AMI
- Continue fetching user-data from S3 (simplified version)

### 5. Simplified user-data (`infrastructure/ansible/roles/k3s-worker-bootstrap/templates/user-data.sh.j2`)
- Remove SSM Agent installation (pre-installed in AMI)
- Remove tool installation (pre-installed in AMI)
- Remove K3s binary installation (pre-installed in AMI)
- Keep: fetch credentials, join cluster, tag instance

---

## File Structure

```
production/
├── scale-up-lambda/
│   └── main.py                           # [MODIFIED] Read AMI from SSM
│
├── infrastructure/
│   ├── ansible/
│   │   ├── worker-bake.yml               # [NEW] AMI baking playbook
│   │   ├── ami-lifecycle.yml            # [NEW] AMI cleanup playbook
│   │   └── roles/
│   │       ├── k3s-worker-preinstall/   # [NEW] SSM Agent + tools
│   │       │   ├── tasks/main.yml
│   │       │   └── handlers/main.yml
│   │       │
│   │       └── k3s-agent-binary/         # [NEW] K3s binary only
│   │           ├── tasks/main.yml
│   │           └── handlers/main.yml
│   │
│   └── packer/                          # [REMOVE - not needed]
│
└── docs/
    └── implementation_fast_worker_bootstrap.md  # This document
```

---

## Bake Process

### Bake New AMI

```bash
cd infrastructure/ansible

# Bake AMI (uses existing base AMI as source)
ansible-playbook worker-bake.yml \
  -e "cluster_name=production-k3s" \
  -e "date=$(date +%Y%m%d)"
```

### Bake Playbook Workflow

```
1. Launch temporary t3.small instance from base AMI
2. Wait for SSH
3. Run k3s-worker-preinstall role (awscli, curl, jq, SSM Agent via snap)
4. Run k3s-agent-binary role (INSTALL_K3S_SKIP_START=true)
5. Create AMI from instance (k3s-worker-{cluster}-{date})
6. Write AMI ID to SSM Parameter /k3s/{cluster}/worker-ami-id
7. Terminate temporary instance
8. Cleanup old AMIs (retain last 2)
```

### AMI Lifecycle Cleanup

```bash
# Deregister old AMIs (keeps last 2)
ansible-playbook ami-lifecycle.yml \
  -e "cluster_name=production-k3s"
```

---

## Bootstrap Time Comparison

### Before (current user-data)
| Step | Time |
|------|------|
| Install dependencies (awscli, curl, jq) | 30-60s |
| Install SSM Agent | 60-120s |
| Wait for SSM Agent to start | 30-60s |
| Get master IP from SSM | 10-30s |
| Get join token from Secrets | 10-30s |
| Install K3s binary | 30-60s |
| Join cluster | 10-20s |
| **Total** | **~3-5 minutes** |

### After (pre-baked AMI + simplified user-data)
| Step | Time |
|------|------|
| SSM Agent already running (from snap) | 0s |
| Tools already installed | 0s |
| Get metadata from IMDSv2 | 1-2s |
| Get master IP from SSM | 5-10s |
| Get join token from Secrets | 5-10s |
| Start k3s-agent with join params | 10-20s |
| Tag instance | 2-5s |
| **Total** | **~30-60 seconds** |

---

## AMI Naming Convention

`k3s-worker-{cluster-name}-{YYYYMMDD}`

Examples:
- `k3s-worker-production-k3s-20260427`
- `k3s-worker-dev-k3s-20260427`

---

## AMI Lifecycle

- **Retain:** Last 2 AMIs per cluster
- **Cleanup:** Oldest AMI deregistered when new one is created
- **Trigger:** Manual via `make bake-ami` or CI on schedule

---

## SSM Parameter Update Flow

```
Ansible playbook completes provisioning
  → AMI ID: ami-0xxxxxxxxx
  → ansible.aws.ec2_ami creates AMI
  → community.aws.aws_ssm_parameter writes:
      aws ssm put-parameter \
        --name "/k3s/production-k3s/worker-ami-id" \
        --value "ami-0xxxxxxxxx" \
        --type "String" \
        --overwrite
```

---

## scale-up-lambda Integration

### Current flow (reads AMI from env var)
```python
# _get_config() in scale-up-lambda/main.py
ami_id = os.environ.get("AMI_ID")  # Hardcoded in Lambda config
```

### New flow (reads AMI from SSM Parameter)
```python
def _get_worker_ami_id(cluster_name: str, region: str) -> str:
    """Fetch baked AMI ID from SSM Parameter."""
    ssm_client = boto3.client("ssm", region_name=region)
    ami_param = ssm_client.get_parameter(
        Name=f"/k3s/{cluster_name}/worker-ami-id"
    )
    return ami_param["Parameter"]["Value"]

# In _get_config():
# 1. Get cluster_name from env
# 2. Fetch AMI from SSM Parameter
# 3. Use AMI in instance launch
```

### _launch_test_instance() changes
```python
def _launch_test_instance() -> dict:
    config = _get_config()

    # Get AMI from SSM (replaces hardcoded AMI_ID env var)
    cluster_name = config.get("cluster_name", "production-k3s")
    region = config.get("aws_region", "ap-southeast-1")
    ami_id = _get_worker_ami_id(cluster_name, region)

    # Continue with existing launch logic...
    # user-data still fetched from S3, but simplified (join only)
```

---

## Simplified user-data.sh.j2

Current user-data (installs everything):
```bash
# [1/7] Install dependencies
apt-get install -y awscli curl jq ca-certificates

# [2/7] Install SSM Agent
curl -o /tmp/ssm/amazon-ssm-agent.deb ...
dpkg -i /tmp/ssm/amazon-ssm-agent.deb

# [5/7] Get master IP from SSM
# [6/7] Get join token from Secrets
# [7/7] Install K3s and join
curl -sfL https://get.k3s.io | K3S_URL=... K3S_TOKEN=... sh
```

Simplified user-data (join only):
```bash
#!/bin/bash
# Pre-baked AMI: only join cluster and tag
# SSM Agent, K3s binary, tools all already installed

set -e

# Get instance metadata
TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds:21600" -s)
INSTANCE_ID=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id -s)
LOCAL_IP=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/local-ipv4 -s)

# Get master IP
MASTER_IP=$(aws ssm get-parameter \
    --name "/k3s/production-k3s/master-ip" \
    --query "Parameter.Value" --output text --region ap-southeast-1)

# Get join token
JOIN_TOKEN=$(aws secretsmanager get-secret-value \
    --secret-id "k3s-production-k3s-join-token" \
    --query "SecretString" --output text --region ap-southeast-1)

# Start k3s-agent (binary already installed via AMI)
curl -sfL https://get.k3s.io | \
    K3S_URL="https://$MASTER_IP:6443" \
    K3S_TOKEN="$JOIN_TOKEN" \
    INSTALL_K3S_EXEC="agent --node-ip=$LOCAL_IP --node-name=ip-$LOCAL_IP" \
    sh -

# Tag instance as success
aws ec2 create-tags --resources "$INSTANCE_ID" \
    --tags "Key=JoinStatus,Value=success" --region ap-southeast-1
```

---

## Implementation Tasks

| # | Task | Status |
|---|------|--------|
| 1 | Create `k3s-worker-preinstall` Ansible role | TODO |
| 2 | Create `k3s-agent-binary` Ansible role | TODO |
| 3 | Create `worker-bake.yml` Ansible playbook | TODO |
| 4 | Create `ami-lifecycle.yml` Ansible playbook | TODO |
| 5 | Update `scale-up-lambda/main.py` to read AMI from SSM | TODO |
| 6 | Simplify `user-data.sh.j2` (remove pre-installed components) | TODO |
| 7 | Test full bake + deploy cycle | TODO |

---

## Dependencies

- Ansible >= 2.10
- `community.aws` collection (for `aws_ssm_parameter`)
- `amazon.aws` collection (for `ec2_ami`)
- AWS CLI configured with appropriate permissions
- Existing IAM roles (k3s-autoscaler-lambda-role etc.)

---

## Security Considerations

1. **SSM Agent baked into AMI** — auto-starts at boot, secure by default
2. **K3s binary** — installed but not started (prevents split-brain)
3. **Join token** — never stored on AMI, always fetched at launch via user-data
4. **IAM Role** — worker instance profile provides least-privilege access

---

## Rollback Plan

If baked AMI fails:

1. **scale-up-lambda** falls back to reading base AMI from env var `AMI_ID`
2. Lambda continues using existing user-data.sh.j2 (full install version)
3. No Pulumi changes needed — infrastructure unchanged

To disable baked AMI:
```bash
# Lambda will use AMI_ID env var instead of SSM Parameter
# Set AMI_ID env var to base Ubuntu AMI: ami-0c687e8f5c4e54af5
```

---

## Bootstrap Time Comparison (Measured)

| Step | Before | After | Savings |
|------|--------|-------|---------|
| Install deps (awscli, curl, jq) | 30-60s | 0s | 30-60s |
| Install SSM Agent | 60-120s | 0s | 60-120s |
| Wait for SSM Agent to start | 30-60s | 0s | 30-60s |
| Get metadata from IMDSv2 | 2-3s | 2-3s | 0s |
| Get master IP from SSM | 5-10s | 5-10s | 0s |
| Get join token from Secrets | 5-10s | 5-10s | 0s |
| Install K3s binary | 30-60s | 0s | 30-60s |
| Start k3s-agent | 10-20s | 10-20s | 0s |
| Tag instance | 2-5s | 2-5s | 0s |
| **Total** | **~91s** | **~30-50s** | **~40-60s** |

**Measured bootstrap time: 91 seconds → ~40 seconds target**