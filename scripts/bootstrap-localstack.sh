#!/usr/bin/env bash
# Pre-create LocalStack resources that the Pulumi stack expects, so the apply
# doesn't race with delete-during-create cycles.
#
# What this script does:
#   1. Creates the 2 S3 buckets (k3s-models, k3s-userdata-production-k3s).
#   2. Creates the 4 DynamoDB tables (k3s-cluster-state, k3s-scaling-wal,
#      k3s-scaling-history, k3s-scaling-metrics-samples) with key schemas that
#      match what __main__.py creates at lines 324, 353, 379, 396.
#   3. Pre-creates the 6 AWS-managed-policy ARNs that __main__.py attaches to
#      IAM roles at lines 536, 543, 778, 785, 823, 830. LocalStack Pro accepts
#      these, but pre-creating makes the apply more deterministic.
#
# Idempotent: re-running is safe. Errors from "already exists" are swallowed.
#
# Usage:
#   source scripts/localstack-env.sh
#   ./scripts/bootstrap-localstack.sh

set -euo pipefail

# We assume the caller has already sourced scripts/localstack-env.sh so that
# AWS_ENDPOINT_URL + dummy creds are set. We could source it ourselves but
# keeping the caller's env intact is friendlier when this runs in a pipeline.

if ! command -v awslocal >/dev/null 2>&1; then
  echo "ERROR: awslocal not on PATH. Source scripts/localstack-env.sh first." >&2
  exit 1
fi

echo "==> [1/3] S3 buckets"
for bucket in k3s-models k3s-userdata-production-k3s; do
  if awslocal s3 ls "s3://${bucket}" >/dev/null 2>&1; then
    echo "    s3://${bucket} already exists — skipping"
  else
    awslocal s3 mb "s3://${bucket}" >/dev/null
    echo "    created s3://${bucket}"
  fi
done

echo "==> [2/3] DynamoDB tables"
create_table() {
  local name="$1" hash_key="$2"
  if awslocal dynamodb describe-table --table-name "$name" >/dev/null 2>&1; then
    echo "    ${name} already exists — skipping"
  else
    awslocal dynamodb create-table \
      --table-name "$name" \
      --attribute-definitions "AttributeName=${hash_key},AttributeType=S" \
      --key-schema "AttributeName=${hash_key},KeyType=HASH" \
      --billing-mode PAY_PER_REQUEST >/dev/null
    echo "    created ${name} (hash=${hash_key})"
  fi
}
# Match __main__.py:  cluster_state (l.324), wal (l.353), scaling_history (l.379),
# metrics_samples (l.396)
create_table k3s-cluster-state        cluster_id
create_table k3s-scaling-wal          operation_id
create_table k3s-scaling-history      decision_id
create_table k3s-scaling-metrics-samples timestamp

echo "==> [3/3] AWS-managed-policy ARNs (pre-create so RolePolicyAttachment succeeds)"
# The Pulumi program attaches these as managed policies. Pre-creating them with
# a wildcard-allow document makes the apply deterministic on LocalStack Pro.
ALL_ALLOW='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'
for arn in \
  arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
  arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole \
  arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore \
  arn:aws:iam::aws:policy/AWSSecretsManagerClientReadOnlyAccess \
  arn:aws:iam::aws:policy/SecretsManagerReadWrite ; do
  if awslocal iam get-policy --policy-arn "$arn" >/dev/null 2>&1; then
    echo "    ${arn} already exists — skipping"
  else
    # --policy-arn lets us claim the canonical ARN. Falls back to local
    # auto-generated ARN if Pro rejects the canonical ARN.
    if awslocal iam create-policy \
        --policy-arn "$arn" \
        --policy-document "$ALL_ALLOW" >/dev/null 2>&1; then
      echo "    created ${arn}"
    else
      echo "    create_policy rejected ${arn} — fallback to auto-ARN"
      awslocal iam create-policy \
        --policy-document "$ALL_ALLOW" >/dev/null 2>&1 || true
    fi
  fi
done

echo
echo "Bootstrap complete. Resources:"
awslocal s3 ls
awslocal dynamodb list-tables --output text 2>/dev/null
echo "IAM managed policies: $(awslocal iam list-policies --scope AWS --query 'Policies[*].PolicyName' --output text 2>/dev/null | tr '\t' ' ')"