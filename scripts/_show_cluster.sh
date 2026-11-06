#!/bin/bash
set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1
aws ec2 describe-instances \
    --filters "Name=tag:Project,Values=elastikube-test" \
    --query 'Reservations[].Instances[].{Name:Tags[?Key==`Name`]|[0].Value,Id:InstanceId,State:State.Name,PublicIP:PublicIpAddress,PrivateIP:PrivateIpAddress,Role:Tags[?Key==`Role`]|[0].Value}' \
    --output table
