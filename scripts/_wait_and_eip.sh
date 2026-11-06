#!/bin/bash
# Wait for the 3 instances to be running, then allocate + attach an EIP to the master.
set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1

echo "[*] Waiting for all 3 instances to be running..."
aws ec2 wait instance-running --instance-ids i-02d59ff22eea20be3 i-0e40a6b67bd8d6828 i-0095df76491a5b890

echo "[*] All 3 instances now running"

# Allocate + attach EIP to the master
echo "[*] Allocating EIP for k3s-master (i-02d59ff22eea20be3)"
ALLOC=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
echo "  AllocationId: $ALLOC"
aws ec2 associate-address --instance-id i-02d59ff22eea20be3 --allocation-id "$ALLOC" --query 'AssociationId' --output text

# Refresh state
echo ""
echo "=== Cluster state ==="
aws ec2 describe-instances \
    --filters "Name=tag:Project,Values=elastikube-test" "Name=instance-state.name,Values=running" \
    --query 'Reservations[].Instances[].{Name:Tags[?Key==`Name`]|[0].Value,Id:InstanceId,PublicIP:PublicIpAddress,PrivateIP:PrivateIpAddress,Role:Tags[?Key==`Role`]|[0].Value}' \
    --output table
