#!/bin/bash
# Re-provision the entire elastikube-test infrastructure from scratch on the
# current poridhi-aws account. Idempotent where it can be; assumes a clean
# AWS account (no leftover elastikube-test resources).
#
# Steps:
#   1. Clean up any leftover probe resources
#   2. VPC + subnets + IGW + route tables + security group + keypair
#   3. IAM instance profile (k3s-worker) wrapped around existing role
#   4. S3 bucket + DynamoDB tables + seed data
#
# After this script completes, run `_aws_launch.sh` to launch EC2s.

set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1
REGION=ap-southeast-1
NAME_PREFIX=elastikube-test
KEY_NAME=elastikube-test
KEY_FILE=/tmp/elastikube-test.pem

echo "============================================================"
echo "  elastikube-test full provisioning on $(aws sts get-caller-identity --query 'Account' --output text)"
echo "============================================================"

# ---- 1. Cleanup leftover probe resources ----
echo "[1/4] Cleaning up probe resources"
for bucket in $(aws s3 ls --output text 2>/dev/null | grep -E 'elastikube-(perm-probe|test)' | awk '{print $NF}'); do
    echo "    Removing s3://$bucket"
    aws s3 rb "s3://$bucket" --force >/dev/null 2>&1 || true
done
for table in $(aws dynamodb list-tables --query 'TableNames' --output text 2>/dev/null | tr '\t' '\n' | grep -E '^(elastikube_perm_probe|k3s-)' | head); do
    echo "    Removing DDB table $table"
    aws dynamodb delete-table --table-name "$table" >/dev/null 2>&1 || true
done

# ---- 2. Keypair ----
echo "[2/4] Provisioning keypair"
if aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    if [ ! -f "$KEY_FILE" ]; then
        echo "    Key exists in AWS but file missing — deleting + recreating"
        aws ec2 delete-key-pair --key-name "$KEY_NAME" >/dev/null
    else
        echo "    Keypair $KEY_NAME + file both present"
    fi
fi
if [ ! -f "$KEY_FILE" ]; then
    echo "    Creating new keypair $KEY_NAME"
    aws ec2 create-key-pair --key-name "$KEY_NAME" --query 'KeyMaterial' --output text > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
fi

# ---- 3. VPC + subnets + IGW + route tables + SG ----
echo "[3/4] Provisioning VPC network"
# Reuse the existing _aws_provision.sh logic
bash /mnt/e/custom_autoscaler/production/scripts/_aws_provision.sh 2>&1 | tail -20

# ---- 4. IAM instance profile ----
echo "[4/4] Provisioning IAM instance profile"
if ! aws iam get-instance-profile --instance-profile-name k3s-worker >/dev/null 2>&1; then
    echo "    Creating instance profile k3s-worker"
    aws iam create-instance-profile --instance-profile-name k3s-worker >/dev/null
fi
PROFILE_ROLES=$(aws iam get-instance-profile --instance-profile-name k3s-worker \
    --query 'InstanceProfile.Roles[].RoleName' --output text 2>/dev/null)
if ! echo "$PROFILE_ROLES" | grep -q "k3s-worker"; then
    echo "    Attaching role k3s-worker to instance profile"
    aws iam add-role-to-instance-profile --instance-profile-name k3s-worker --role-name k3s-worker >/dev/null
fi

# ---- Summary ----
echo ""
echo "============================================================"
echo "  Provisioning complete"
echo "============================================================"
echo "VPC_ID=$(grep '^VPC_ID=' /mnt/e/custom_autoscaler/production/scripts/elastikube-test-state.env | cut -d= -f2)"
echo "Public subnet  = $(grep '^PUBLIC_SUBNET_ID=' /mnt/e/custom_autoscaler/production/scripts/elastikube-test-state.env | cut -d= -f2)"
echo "Private subnet = $(grep '^PRIVATE_SUBNET_ID=' /mnt/e/custom_autoscaler/production/scripts/elastikube-test-state.env | cut -d= -f2)"
echo "Key file = $KEY_FILE"
echo "Instance profile = k3s-worker (role: k3s-worker)"
