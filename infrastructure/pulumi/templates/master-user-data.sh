#!/bin/bash
# K3s master bootstrap. Rendered by Pulumi (bootstrap.py) and run once
# by cloud-init on the master's first boot. Replaces the k3s-master Ansible role
# in the bring-up path.
#
# The join token is never part of this script: user-data is readable through
# DescribeInstanceAttribute, so the token is read from Secrets Manager at boot.
set -euo pipefail
exec > >(tee -a /var/log/k3s-master-bootstrap.log) 2>&1

REGION="__REGION__"
SECRET_ID="__SECRET_ID__"
BUCKET="__BUCKET__"
K3S_VERSION="__K3S_VERSION__"
NODE_IP="__NODE_IP__"
MANIFEST_DIR=/var/lib/rancher/k3s/server/manifests

echo "$(date -u +%FT%TZ) master bootstrap start"

echo "[1/5] Ensuring AWS CLI..."
if ! command -v aws >/dev/null 2>&1; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q awscli
fi

echo "[2/5] Reading join token from Secrets Manager..."
TOKEN=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" \
  --query SecretString --output text)
if [ -z "$TOKEN" ] || [ "$TOKEN" = "PENDING" ]; then
  echo "ERROR: join token secret is empty or still PENDING" >&2
  exit 1
fi

# k3s applies every manifest in this directory at startup and on change, so
# the monitoring stack needs no kubectl step.
echo "[3/5] Fetching monitoring manifests and helper script..."
mkdir -p "$MANIFEST_DIR"
aws s3 cp --region "$REGION" --recursive "s3://$BUCKET/master/manifests/" "$MANIFEST_DIR/"
aws s3 cp --region "$REGION" "s3://$BUCKET/master/check-node-by-ip.sh" /usr/local/bin/check-node-by-ip.sh
chmod 0755 /usr/local/bin/check-node-by-ip.sh

echo "[4/5] Installing k3s server $K3S_VERSION..."
curl -sfL --retry 5 --retry-delay 10 https://get.k3s.io -o /tmp/k3s-install.sh
for attempt in 1 2 3; do
  if INSTALL_K3S_VERSION="$K3S_VERSION" K3S_TOKEN="$TOKEN" sh /tmp/k3s-install.sh server \
      --node-ip "$NODE_IP" --tls-san "$NODE_IP" \
      --write-kubeconfig-mode 644 \
      --node-taint node-role.kubernetes.io/control-plane=true:NoSchedule; then
    break
  fi
  if [ "$attempt" -eq 3 ]; then
    echo "ERROR: k3s install failed after 3 attempts" >&2
    exit 1
  fi
  sleep 15
done
unset TOKEN

# The scale-down and cleanup Lambdas run kubectl on the master through SSM.
echo "[5/5] Ensuring SSM Agent..."
if ! snap list amazon-ssm-agent >/dev/null 2>&1 && ! dpkg -s amazon-ssm-agent >/dev/null 2>&1; then
  curl -sfL --retry 5 --max-time 600 -o /tmp/amazon-ssm-agent.deb \
    https://s3.amazonaws.com/ec2-downloads-windows/SSMAgent/latest/debian_amd64/amazon-ssm-agent.deb
  dpkg -i /tmp/amazon-ssm-agent.deb
fi
systemctl enable --now amazon-ssm-agent 2>/dev/null || snap start amazon-ssm-agent 2>/dev/null || true

echo "$(date -u +%FT%TZ) master bootstrap complete"
