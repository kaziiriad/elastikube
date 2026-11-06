#!/bin/bash
# Seed the elastikube ML training DynamoDB tables. Goes straight to real AWS.
# 7 days = 5,040 metrics + ~219 history rows. Smaller than 30 days (faster),
# enough for Prophet to fit meaningfully.
set -euo pipefail
unset AWS_ENDPOINT_URL AWS_ENDPOINT_URL_S3 AWS_ENDPOINT_URL_DYNAMODB \
      AWS_ENDPOINT_URL_IAM AWS_ENDPOINT_URL_LAMBDA AWS_ENDPOINT_URL_EC2 \
      AWS_ENDPOINT_URL_STS
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1
python3 /mnt/e/custom_autoscaler/production/scripts/seed_ml_tables.py \
    --days 7 \
    --region ap-southeast-1
