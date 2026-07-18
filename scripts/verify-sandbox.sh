#!/usr/bin/env bash
# Verify the LocalStack + Pulumi half of the elastikube sandbox.
#
# Scope: this script validates that the Pulumi stack at infrastructure/pulumi
# provisions correctly into a running LocalStack Pro container. It does NOT
# validate the ML CronJob end-to-end (that requires a kind cluster — see
# /home/poridhian/.puku-cli/plans/graceful-purring-bengio.md).
#
# Prints PASS/FAIL per item and exits 1 if any check fails.
#
# Usage:
#   source scripts/localstack-env.sh
#   ./scripts/verify-sandbox.sh

set -uo pipefail

# Resolve the repo root regardless of where the script is invoked from
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PASS=0
FAIL=0
RESULTS=()

check() {
  local num="$1" desc="$2" cmd="$3"
  if eval "$cmd" >/tmp/verify-check.out 2>&1; then
    PASS=$((PASS + 1))
    RESULTS+=("PASS  #${num} ${desc}")
  else
    FAIL=$((FAIL + 1))
    RESULTS+=("FAIL  #${num} ${desc}")
    RESULTS+=("        $(head -c 240 /tmp/verify-check.out | tr '\n' ' ')")
  fi
}

# 1. LocalStack up — health endpoint reachable
check 1 "LocalStack health endpoint responds" \
  "curl -sf http://localhost:4566/_localstack/health >/dev/null"

# 2. Required services available (accept either 'available' or 'running' — both
# mean the service is up and ready)
check 2 "All target services available (s3, dynamodb, iam, lambda, ec2)" \
  "curl -sf http://localhost:4566/_localstack/health | python3 -c \"
import json, sys
d = json.load(sys.stdin)
needed = ['s3','dynamodb','iam','lambda','ssm','secretsmanager','cloudwatch','ec2','sts','events','sns','sqs']
ok = all(d.get('services', {}).get(k) in ('available', 'running') for k in needed)
sys.exit(0 if ok else 1)
\""

# 4. Bootstrap script has been run — DDB tables exist
check 4 "Bootstrap ran: DynamoDB tables present (k3s-cluster-state, k3s-scaling-wal, k3s-scaling-history, k3s-scaling-metrics-samples)" \
  "for t in k3s-cluster-state k3s-scaling-wal k3s-scaling-history k3s-scaling-metrics-samples; do awslocal dynamodb describe-table --table-name \$t >/dev/null || exit 1; done"

# 5. Bootstrap script has been run — S3 buckets exist
check 5 "Bootstrap ran: S3 buckets present (k3s-models, k3s-userdata-production-k3s)" \
  "awslocal s3 ls >/tmp/s3.out 2>&1 && grep -q k3s-models /tmp/s3.out && grep -q k3s-userdata-production-k3s /tmp/s3.out"

# 6. Pulumi localstack stack exists
check 6 "Pulumi 'localstack' stack is initialized" \
  "cd \"${REPO_ROOT}/infrastructure/pulumi\" && pulumi stack ls 2>/dev/null | grep -q '^localstack'"

# 7. Pulumi apply succeeded — stack has outputs
check 7 "Pulumi stack has outputs (cluster_name, lambda_function_arn, etc.)" \
  "cd \"${REPO_ROOT}/infrastructure/pulumi\" && pulumi stack output --stack localstack >/dev/null 2>&1"

# 8. Decision lambda function exists
check 8 "Decision lambda 'k3s-autoscaler' is registered (python3.11 runtime)" \
  "awslocal lambda get-function --function-name k3s-autoscaler --query 'Configuration.Runtime' --output text 2>/dev/null | grep -q python3.11"

# 9. Lambda code is loadable — invoke the decision lambda
check 9 "Decision lambda invocation returns 200" \
  "awslocal lambda invoke --function-name k3s-autoscaler --payload '{\"cluster\":\"production-k3s\"}' /tmp/lambda-out.json >/dev/null 2>&1 && grep -q 'StatusCode.*200' /tmp/lambda-out.json"

# 10. Predictive path fired — Lambda log shows model loaded message
check 10 "Predictive-scaling path fires (Lambda logs contain 'Prophet model loaded' or 'model loaded successfully')" \
  "(grep -q 'Prophet model loaded' /tmp/localstack/logs/lambda.log 2>/dev/null || grep -q 'model loaded successfully' /tmp/localstack/logs/lambda.log 2>/dev/null)"

# 11. k3s-models bucket is writable — round-trip a small object
check 11 "k3s-models bucket is writable (round-trip via awslocal s3 cp)" \
  "echo 'sandbox-roundtrip-ok' > /tmp/rt.txt && awslocal s3 cp /tmp/rt.txt s3://k3s-models/sandbox-rt.txt >/dev/null 2>&1 && awslocal s3 cp s3://k3s-models/sandbox-rt.txt - 2>/dev/null | grep -q 'sandbox-roundtrip-ok'"

echo
printf '%s\n' "${RESULTS[@]}"
echo
echo "Summary: ${PASS} passed, ${FAIL} failed"
if [[ ${FAIL} -gt 0 ]]; then
  exit 1
fi