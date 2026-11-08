#!/bin/bash
# K3s worker bootstrap. Rendered by Pulumi (bootstrap.py) into the worker
# Launch Template; every worker (permanent and Lambda-launched) runs it once on
# first boot. On a pre-baked AMI it only joins and tags; on a plain Ubuntu AMI
# it first installs the AWS CLI and the pinned k3s binary.
#
# The join token is never part of this script: user-data is readable through
# DescribeInstanceAttribute, so the token is read from Secrets Manager at boot.

set -e

CLUSTER_NAME="__CLUSTER_NAME__"
AWS_REGION="__REGION__"
K3S_VERSION="__K3S_VERSION__"
SECRET_NAME="k3s-${CLUSTER_NAME}-join-token"
SSM_MASTER_IP="/k3s/${CLUSTER_NAME}/master-ip"

# Boot-phase timing: when user-data started and how long the kernel had been
# up by then. Written as tags with JoinStatus so the scale-up Lambda can split
# launch -> joined into EC2 launch / OS boot / bootstrap phases.
BOOTSTRAP_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BOOT_UPTIME_AT_START=$(cut -d' ' -f1 /proc/uptime)

echo "=== K3s Worker Node Bootstrap ==="
echo "Cluster: $CLUSTER_NAME"
echo "Region: $AWS_REGION"

# Plain Ubuntu AMIs ship neither the AWS CLI nor k3s; a baked AMI has both.
if ! command -v aws >/dev/null 2>&1; then
    echo "Installing AWS CLI (not in AMI)..."
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q awscli
fi

# Get instance metadata from IMDSv2
echo "[1/6] Getting instance metadata..."
TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds:21600" -s)
INSTANCE_ID=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id -s)
LOCAL_IP=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/local-ipv4 -s)
AZ=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/availability-zone -s)
echo "  Instance: $INSTANCE_ID"
echo "  IP: $LOCAL_IP"
echo "  AZ: $AZ"

# Read EC2 tags to determine if this worker is permanent (k3s-worker-1/2)
# and to capture its `Name` value, which we'll apply as a Kubernetes node
# label via `k3s agent --node-label`. This is what makes workloads pinned
# via `nodeAffinity: { k3s-worker In [k3s-worker-1, k3s-worker-2] }` (e.g.
# the ml-training CronJob) actually schedule on permanent workers.
echo "[2/6] Reading worker identity tags from EC2..."
WORKER_NAME=""
WORKER_PERMANENT="false"
TAGS_JSON=$(aws ec2 describe-tags \
    --region "$AWS_REGION" \
    --filters "Name=resource-id,Values=$INSTANCE_ID" \
    --output json 2>/dev/null || echo '{"Tags":[]}')

# Extract Name and Permanent from the tag set. Use python3 because the
# workers' pre-baked AMI ships it (k3s-worker-bake.yml) and avoids a
# fragile jq dependency in this single call site.
eval "$(echo "$TAGS_JSON" | python3 -c '
import json, sys
data = json.load(sys.stdin)
out = {"WORKER_NAME": "", "WORKER_PERMANENT": "false"}
for tag in data.get("Tags", []):
    key, val = tag.get("Key", ""), tag.get("Value", "")
    if key == "Name":
        out["WORKER_NAME"] = val
    elif key == "Permanent" and val.lower() == "true":
        out["WORKER_PERMANENT"] = "true"
for k, v in out.items():
    print(f"{k}=\"{v}\"")
')"

if [ "$WORKER_PERMANENT" = "true" ] && [ -n "$WORKER_NAME" ]; then
    echo "  Permanent worker detected: name=$WORKER_NAME (will set k8s label)"
else
    echo "  Ephemeral worker (no Permanent=true tag or no Name)"
fi

# Get master IP from SSM Parameter Store
echo "[3/6] Fetching master IP from SSM Parameter..."
MAX_RETRIES=5
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    MASTER_IP=$(
        aws ssm get-parameter \
            --name "$SSM_MASTER_IP" \
            --region "$AWS_REGION" \
            --query "Parameter.Value" \
            --output text 2>&1
    )

    if [ $? -eq 0 ] && [ -n "$MASTER_IP" ]; then
        echo "  Master IP: $MASTER_IP"
        break
    fi

    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo "  Retry $RETRY_COUNT/$MAX_RETRIES: Failed to get master IP"
    sleep 5
done

if [ -z "$MASTER_IP" ]; then
    echo "ERROR: Failed to get master IP from SSM after $MAX_RETRIES attempts"
    exit 1
fi

# Get K3s join token from Secrets Manager
echo "[4/6] Fetching K3s join token from Secrets Manager..."
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    JOIN_TOKEN=$(
        aws secretsmanager get-secret-value \
            --secret-id "$SECRET_NAME" \
            --region "$AWS_REGION" \
            --query "SecretString" \
            --output text 2>&1
    )

    if [ $? -eq 0 ] && [ -n "$JOIN_TOKEN" ]; then
        echo "  Token retrieved"
        break
    fi

    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo "  Retry $RETRY_COUNT/$MAX_RETRIES: Failed to get join token"
    sleep 5
done

if [ -z "$JOIN_TOKEN" ]; then
    echo "ERROR: Failed to get join token from Secrets Manager after $MAX_RETRIES attempts"
    exit 1
fi

# Detect primary network interface (ens5 on ENA, eth0 on virtio)
PRIMARY_IFACE=$(ip route show default | awk '{print $5}' | head -1)
if [ -z "$PRIMARY_IFACE" ]; then
    PRIMARY_IFACE="ens5"
fi
echo "  Primary interface: $PRIMARY_IFACE"

echo "[5/6] Starting k3s agent..."

# Safety: stop any k3s server/agent that auto-started from AMI
echo "  Stopping any auto-started k3s services..."
systemctl stop k3s 2>/dev/null || true
systemctl stop k3s-agent 2>/dev/null || true
systemctl disable k3s 2>/dev/null || true
systemctl disable k3s-agent 2>/dev/null || true
pkill -9 -f "k3s" 2>/dev/null || true
# Wait (max 5s) for killed processes to exit instead of a fixed sleep.
for _ in $(seq 1 50); do
    pgrep -f "k3s" > /dev/null 2>&1 || break
    sleep 0.1
done

# Install the pinned k3s binary unless the AMI already has one
if [ -f /usr/local/bin/k3s ] && [ ! -x /usr/local/bin/k3s ]; then
    echo "Making k3s binary executable..."
    chmod +x /usr/local/bin/k3s
fi
if [ ! -x /usr/local/bin/k3s ]; then
    echo "  Downloading k3s $K3S_VERSION (not in AMI)..."
    curl -sfL --retry 5 --retry-delay 10 --max-time 900 -o /usr/local/bin/k3s \
        "https://github.com/k3s-io/k3s/releases/download/__K3S_VERSION_URL__/k3s"
    chmod +x /usr/local/bin/k3s
fi

# Wait for the master API before the join clock starts. At bring-up the
# permanent workers boot alongside the master, which takes minutes to install
# k3s; Lambda-launched workers find it already up and pass straight through.
# -k: the k3s API uses a self-signed cert. No -f: any HTTP answer (e.g. 401)
# proves the API is reachable; only a connection failure is non-zero.
MASTER_WAIT_TIMEOUT=600
echo "Waiting for master API $MASTER_IP:6443 (max ${MASTER_WAIT_TIMEOUT}s)..."
MASTER_WAIT_START=$(date +%s)
until curl -sk -o /dev/null --connect-timeout 5 "https://$MASTER_IP:6443"; do
    if [ $(( $(date +%s) - MASTER_WAIT_START )) -ge $MASTER_WAIT_TIMEOUT ]; then
        echo "  WARNING: master API still unreachable - starting agent anyway"
        break
    fi
    sleep 5
done
echo "  Master API check done after $(( $(date +%s) - MASTER_WAIT_START ))s"

echo "Starting k3s-agent..."

# Permanent workers carry an extra `--node-label` so the matching
# Kubernetes label is set at registration time (no post-boot kubectl
# step needed). Ephemeral workers get no extra label.
NODE_LABEL_ARGS=()
if [ "$WORKER_PERMANENT" = "true" ] && [ -n "$WORKER_NAME" ]; then
    NODE_LABEL_ARGS=(--node-label "k3s-worker=$WORKER_NAME")
    echo "  Applying k3s-worker=$WORKER_NAME label at registration"
fi

AGENT_ARGS=(
    --server="https://$MASTER_IP:6443"
    --node-ip="$LOCAL_IP"
    --node-name="ip-$LOCAL_IP"
    --with-node-id
    --flannel-iface="$PRIMARY_IFACE"
    "${NODE_LABEL_ARGS[@]}"
)

# The token reaches the agent through K3S_TOKEN in a root-only env file: not
# on the command line (visible in ps) and not in the unit file (world-readable).
(umask 077 && printf 'K3S_TOKEN=%s\n' "$JOIN_TOKEN" > /etc/k3s-agent.env)
chmod 600 /etc/k3s-agent.env
unset JOIN_TOKEN

if [ -d /run/systemd/system ]; then
    # cloud-init runs user-data only on first boot, so the agent must be an
    # enabled unit to come back after a reboot.
    EXEC_ARGS=$(printf '"%s" ' "${AGENT_ARGS[@]}")
    cat > /etc/systemd/system/k3s-agent.service <<UNIT
[Unit]
Description=k3s agent (ElastiKube worker)
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
EnvironmentFile=/etc/k3s-agent.env
ExecStart=/usr/local/bin/k3s agent ${EXEC_ARGS}
Restart=always
RestartSec=5
KillMode=process
Delegate=yes
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
StandardOutput=append:/var/log/k3s-agent.log
StandardError=append:/var/log/k3s-agent.log

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable --now k3s-agent
    echo "  k3s-agent started as an enabled systemd unit"
    agent_running() { systemctl is-active --quiet k3s-agent; }
else
    # No systemd (minimal images): run directly. Does not survive a reboot.
    set -a; . /etc/k3s-agent.env; set +a
    nohup /usr/local/bin/k3s agent "${AGENT_ARGS[@]}" >> /var/log/k3s-agent.log 2>&1 &
    K3S_PID=$!
    unset K3S_TOKEN
    echo "  k3s-agent started with PID: $K3S_PID (no systemd)"
    agent_running() { kill -0 "$K3S_PID" 2>/dev/null; }
fi

# Wait for the node to actually join. A running agent process is not proof:
# with a wrong token, a rejected node password or an unreachable master the
# agent keeps retrying forever. The kubelet only starts once the server has
# accepted the agent and handed it its config, so a healthy kubelet
# (127.0.0.1:10248/healthz) means the join succeeded. Must stay below the
# scale-up Lambda's BOOTSTRAP_TIMEOUT_SECONDS (default 180s).
JOIN_TIMEOUT=90
echo "  Waiting for kubelet to become healthy (max ${JOIN_TIMEOUT}s)..."
JOINED=false
JOIN_START=$(date +%s)
while [ $(( $(date +%s) - JOIN_START )) -lt $JOIN_TIMEOUT ]; do
    if ! agent_running; then
        echo "  k3s-agent exited"
        break
    fi
    if curl -sf --max-time 2 http://127.0.0.1:10248/healthz > /dev/null 2>&1; then
        JOINED=true
        echo "  kubelet healthy after $(( $(date +%s) - JOIN_START ))s"
        break
    fi
    sleep 1
done

if [ "$JOINED" = "true" ]; then
    echo "[6/6] Tagging instance..."

    # Tag the instance with cluster metadata
    aws ec2 create-tags \
        --resources "$INSTANCE_ID" \
        --tags \
            "Key=Cluster,Value=$CLUSTER_NAME" \
            "Key=NodeRole,Value=worker" \
            "Key=JoinStatus,Value=success" \
            "Key=JoinedAt,Value=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            "Key=BootstrapStartedAt,Value=$BOOTSTRAP_STARTED_AT" \
            "Key=BootUptimeAtStart,Value=$BOOT_UPTIME_AT_START" \
        --region "$AWS_REGION" 2>/dev/null || true

    echo "=== Bootstrap Complete ==="
    echo "Worker node successfully joined cluster '$CLUSTER_NAME'!"
else
    echo "ERROR: node did not join (kubelet not healthy within ${JOIN_TIMEOUT}s, or agent exited)"
    echo "Checking logs:"
    tail -30 /var/log/k3s-agent.log 2>/dev/null || echo "  No log file found"

    # Tag as failed
    aws ec2 create-tags \
        --resources "$INSTANCE_ID" \
        --tags \
            "Key=Cluster,Value=$CLUSTER_NAME" \
            "Key=NodeRole,Value=worker" \
            "Key=JoinStatus,Value=failed" \
            "Key=FailedAt,Value=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            "Key=BootstrapStartedAt,Value=$BOOTSTRAP_STARTED_AT" \
            "Key=BootUptimeAtStart,Value=$BOOT_UPTIME_AT_START" \
        --region "$AWS_REGION" 2>/dev/null || true
    exit 1
fi