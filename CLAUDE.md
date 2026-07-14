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

> All commands below assume your working directory is the repository root. The repo is
> named `elastikube/` on GitHub but lives under `production/` on the original author's
> local checkout — both layouts share the same internal structure, so paths like
> `decision-lambda/` and `infrastructure/pulumi/` work in either setup.

### Building Lambdas

```bash
# Decision Lambda
cd decision-lambda && ./build.sh

# Scale-Up Lambda
cd scale-up-lambda && ./build.sh

# Scale-Down Lambda
cd scale-down-lambda && ./build.sh

# Cleanup Lambda
cd cleanup-lambda && ./build.sh
```

Each `build.sh` creates a `build/lambda.zip` deployment package using `uv` for dependency management.

### Infrastructure Deployment

```bash
cd infrastructure/pulumi

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
cd decision-lambda
uv sync --frozen --no-dev
pytest tests/
```

### ML Training

```bash
cd ml_training
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
| `infrastructure/ansible/roles/ml-training-cronjob/` | CronJob deployment (build image → distribute → apply) |
| `ml_training/scripts/` | Extract → train → validate pipeline (run inside CronJob container) |
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

## ML Training CronJob (`fix/ml-training-cronjob` branch)

Weekly CronJob that trains a Prophet model on the last 30 days of CPU/memory metrics and uploads it to S3. The Decision Lambda later fetches the trained model for predictive scaling.

### Pipeline (runs inside the CronJob container)
1. `extract_data.py` — scans `k3s-scaling-metrics-samples` and `k3s-scaling-history` from DynamoDB
2. `train_model.py` — fits Prophet, saves JSON model + `_metrics.json` to `/app/models/`
3. `validate_model.py` — backtest + per-hour/per-day metrics
4. `aws s3 cp` — uploads versioned model, `cpu_prophet_model.json` (latest), and metrics to `s3://k3s-models/models/`
5. `aws cloudwatch put-metric-data` — publishes MAE/RMSE to `K3sAutoscalerML` namespace

### Key Bug Fixes (this branch)
- **CronJob YAML indentation**: `image:`, `imagePullPolicy:`, `command:` were indented 2 spaces too far, making `kubectl apply` reject the manifest. Fixed and split into `command: [/bin/bash, -c]` + `args:` block for clean YAML.
- **Missing AWS credentials in container**: container had no AWS env vars, so `aws s3 cp` failed with "Unable to locate credentials". Added `envFrom: secretRef: aws-credentials` when access keys are provided.
- **Duplicate Secret declaration**: `aws-credentials` was being created both in `deploy-cronjob.yml` and at the end of the cronjob template. Removed the duplicate from the template (Ansible task is authoritative, uses `no_log: true`).
- **extract_data.py arg mismatch**: CronJob passed `--state-table`/`--wal-table`/`--output-path`, but the script accepts `--metrics-table`/`--history-table`/`--output-dir`. Removed the bogus overrides so the script's defaults (`k3s-scaling-metrics-samples`, `k3s-scaling-history`) are used.
- **train_model.py metric keys**: CronJob read `.validation_mae`/`.validation_rmse` from metrics JSON, but the script only saved `mae`/`rmse`. Added `validation_mae`/`validation_rmse`/`validation_mape` keys alongside the originals.
- **Docker context path brittleness**: `defaults/main.yml` had hardcoded `../../ml_training` (broken from any cwd other than `infrastructure/ansible/`). Changed to `{{ role_path }}/../../../../ml_training` so paths resolve from the role's location, regardless of where the playbook is invoked (pulumi/, ansible/, ./).

### Local Simulation (floci-cli)
- `floci start --pull always` boots a local AWS emulator on `:4566`
- `floci env` prints the `AWS_ENDPOINT_URL`/`AWS_ACCESS_KEY_ID`/etc. shell exports
- Create the S3 bucket once: `eval "$(floci env)" && aws --endpoint-url "$AWS_ENDPOINT_URL" s3 mb s3://k3s-models`
- DynamoDB tables (`k3s-scaling-metrics-samples`, `k3s-scaling-history`) must exist in `ap-southeast-1` (the script's default region) — the CronJob runs the container with no region override, so the table region matters.
- Build & run locally:
  ```sh
  ANSIBLE_ROLES_PATH=infrastructure/ansible/roles ansible-playbook \
    -i infrastructure/ansible/inventory/hosts.ini site.yml \
    -e deploy_ml_training=true --limit=localhost
  ```
- To test the inner pipeline (extract → train → S3 push) without k3s:
  ```sh
  # Pin region to ap-southeast-1 so it matches extract_data.py's default and the
  # DynamoDB tables created above. Use a floci profile (no k3s-temp-user locally).
  docker run --rm --network=host \
    -e AWS_ENDPOINT_URL=http://localhost:4566 \
    -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID \
    -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
    -e AWS_DEFAULT_REGION=ap-southeast-1 \
    -v $(pwd)/ml_training:/app \
    -v /tmp/ml-test:/workspace \
    k3s-ml-training:latest bash <pipeline.sh>
  ```
  Inside the container, write a minimal floci profile so `extract_data.py --profile floci`
  resolves:
  ```sh
  mkdir -p /root/.aws
  printf '[floci]\naws_access_key_id = %s\naws_secret_access_key = %s\n' \
    "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" > /root/.aws/credentials
  printf '[profile floci]\nregion = %s\noutput = json\n' "$AWS_DEFAULT_REGION" \
    > /root/.aws/config
  ```

### Outstanding
- **Validated end-to-end against floci (this session):** `s3://k3s-models` bucket creation,
  DynamoDB tables (`k3s-scaling-metrics-samples`, `k3s-scaling-history`) in
  `ap-southeast-1`, `extract_data.py` → CSV → `train_model.py` → 3× `aws s3 cp`
  (versioned model + `cpu_prophet_model.json` latest + versioned `_metrics.json`).
  Final listing: `cpu_prophet_model.json`, `cpu_prophet_model_<UTC ts>.json`,
  `<UTC ts>_metrics.json`.
- **Fixed in this session:**
  - `train_model.py` validation step used `make_future_dataframe(freq='2min')`, which
    produced zero matches against validation rows at any cadence other than 2 min,
    silently writing an empty `_metrics.json`. Now uses `val_df[['ds']]` directly so
    real MAE/RMSE/MAPE are computed for whatever cadence the input has.
  - `train_model.py` final summary used `f"{metrics.get(k, 'N/A'):.2f}"`, which crashed
    with `ValueError: Unknown format code 'f' for object of type 'str'` whenever the
    metric was missing. Now guarded with a small `_fmt()` helper that handles strings.
- **Open issues (still pending):**
  - DynamoDB seeding script for floci (`/tmp/seed_dynamo.py` on host, regenerated each
    run) is not committed — for local tests only. Lives at `/tmp/seed_dynamo.py`; uses
    `Decimal` for floats (boto3 rejects `float`).
  - Pandas 2.x quirk in `extract_data.py` rejects ISO timestamps with microseconds
    (`+00:00.123456`). Seed data must use second-precision timestamps.
  - `extract_data.py` defaults `--profile=k3s-temp-user`. For local runs without a
    real `k3s-temp-user` profile, create a profile in `/root/.aws/{credentials,config}`
    inside the container (e.g. `[profile floci]`) and pass `--profile floci`.
  - CloudWatch `put-metric-data` against floci silently no-ops; not validated
    end-to-end here. Requires real AWS.
  - `infrastructure/pulumi/__main__.py` does not create the `k3s-models` S3 bucket and
    does not wire `PROPHET_MODEL_S3_BUCKET` / `PROPHET_MODEL_S3_KEY` /
    `PREDICTIVE_SCALING_ENABLED` into the decision Lambda env, so `predictive.py`
    always returns `None`. Decision Lambda predictive scaling is therefore disabled
    in production until this is added.
  - CronJob `nodeSelector: k3s-worker: "k3s-worker-1,k3s-worker-2"` is invalid —
    `nodeSelector` matches one label value, not a comma list. Pods stay `Pending`.
    Use `nodeAffinity` (already commented in the template) for real two-node pinning.
  - Ansible inventory and `ansible.cfg` hard-code `MyKeyPair` / `~/.ssh/MyKeyPair.pem`;
    blocks portability for non-localhost runs.