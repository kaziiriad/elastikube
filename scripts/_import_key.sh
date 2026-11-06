#!/bin/bash
set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1
ssh-keygen -y -f /tmp/elastikube-test.pem > /tmp/elastikube-test.pub
chmod 644 /tmp/elastikube-test.pub
ls -la /tmp/elastikube-test.pub
echo "---"
aws ec2 import-key-pair \
    --key-name elastikube-test \
    --public-key-material "fileb:///tmp/elastikube-test.pub" \
    --query 'KeyName' --output text 2>&1 | head -3
echo "---"
aws ec2 describe-key-pairs --key-names elastikube-test --query 'KeyPairs[].KeyName' --output text 2>&1 | head -1
