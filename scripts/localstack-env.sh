#!/usr/bin/env bash
# Source this file to point AWS CLI / boto3 / pulumi at the local LocalStack Pro
# instance running in the localstack-main Docker container (endpoint :4566).
#
# Replaces `eval "$(floci env)"` from the floci-based workflow documented in
# CLAUDE.md §"Local Simulation".
#
# Usage:
#   source scripts/localstack-env.sh
#   awslocal s3 ls
#   pulumi up
#
# Why localhost (not host.docker.internal): the localstack-main container binds
# 4566 to the host via network_mode=host (see sandbox/docker-compose.yml), so
# the host reaches it as localhost:4566 and so do other host-network-mode
# containers (kind with --network=host). For bridge-mode containers, use
# `--add-host=host.docker.internal:host-gateway` (or rely on it via Docker
# Desktop) — but the simplest path is host networking.

# Export AWS endpoint + dummy creds accepted by LocalStack
export AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://localhost:4566}"
export AWS_ENDPOINT_URL_S3="${AWS_ENDPOINT_URL_S3:-http://localhost:4566}"
export AWS_ENDPOINT_URL_DYNAMODB="${AWS_ENDPOINT_URL_DYNAMODB:-http://localhost:4566}"
export AWS_ENDPOINT_URL_IAM="${AWS_ENDPOINT_URL_IAM:-http://localhost:4566}"
export AWS_ENDPOINT_URL_LAMBDA="${AWS_ENDPOINT_URL_LAMBDA:-http://localhost:4566}"
export AWS_ENDPOINT_URL_EC2="${AWS_ENDPOINT_URL_EC2:-http://localhost:4566}"
export AWS_ENDPOINT_URL_STS="${AWS_ENDPOINT_URL_STS:-http://localhost:4566}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
# LocalStack Pro accepts any region string, but the rest of the codebase
# (extract_data.py, Pulumi config, etc.) expects ap-southeast-1
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
export AWS_REGION="${AWS_REGION:-ap-southeast-1}"
# pulumi-aws reads AWS_PROFILE; clear it so env-var creds above take precedence
unset AWS_PROFILE
# Disable CLI pager
export AWS_PAGER=""
# Pulumi: use local file backend (no S3) and set a fixed passphrase for the
# `localstack` stack so we don't get prompted on every command
export PULUMI_CONFIG_PASSPHRASE="${PULUMI_CONFIG_PASSPHRASE:-localstack}"

# Convenience: `awslocal` wraps `aws --endpoint-url $AWS_ENDPOINT_URL`.
# This script does NOT require awscli-local to be installed; we define a shell
# function instead so the script has no pip dependency. If `awslocal` is on
# PATH (e.g. via `pip install awscli-local`), use that directly.
if ! command -v awslocal >/dev/null 2>&1; then
  awslocal() {
    aws --endpoint-url "${AWS_ENDPOINT_URL}" "$@"
  }
  export -f awslocal
fi

echo "[localstack-env] AWS endpoint: $AWS_ENDPOINT_URL  region: $AWS_DEFAULT_REGION"