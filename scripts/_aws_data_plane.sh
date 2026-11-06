#!/bin/bash
# Create the S3 bucket and DynamoDB tables that the ml-training pipeline
# needs. Idempotent — safe to re-run.
set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1
REGION=ap-southeast-1
BUCKET=k3s-models

echo "============================================================"
echo "  Provisioning elastikube data plane"
echo "============================================================"

# --- S3 bucket ---
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    echo "[=] Bucket s3://$BUCKET already exists"
else
    echo "[+] Creating s3://$BUCKET"
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
        --create-bucket-configuration "LocationConstraint=$REGION" \
        >/dev/null
fi

# Versioning (matches Pulumi's k3s-models bucket config)
VS=$(aws s3api get-bucket-versioning --bucket "$BUCKET" \
    --query 'Status' --output text 2>/dev/null || echo None)
if [ "$VS" != "Enabled" ]; then
    echo "[+] Enabling versioning on s3://$BUCKET"
    aws s3api put-bucket-versioning --bucket "$BUCKET" \
        --versioning-configuration Status=Enabled
fi

# --- DynamoDB tables ---
create_table() {
    local name=$1
    local hash_key=$2
    if aws dynamodb describe-table --table-name "$name" --region "$REGION" >/dev/null 2>&1; then
        echo "[=] Table $name already exists"
    else
        echo "[+] Creating DynamoDB table $name (hash=$hash_key)"
        aws dynamodb create-table --region "$REGION" \
            --table-name "$name" \
            --attribute-definitions "AttributeName=$hash_key,AttributeType=S" \
            --key-schema "AttributeName=$hash_key,KeyType=HASH" \
            --billing-mode PAY_PER_REQUEST >/dev/null
    fi
}

create_table k3s-cluster-state cluster_id
create_table k3s-scaling-wal operation_id
create_table k3s-scaling-metrics-samples timestamp
create_table k3s-scaling-history decision_id

echo ""
echo "============================================================"
echo "  Data plane ready"
echo "============================================================"
aws s3 ls s3://$BUCKET/ 2>&1 | head -3
aws dynamodb list-tables --region "$REGION" --query 'TableNames' --output text 2>&1
