#!/bin/bash
set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1
# Updated instance IDs from this run
MASTER=i-079b681f6985536a4
WORKER1=i-09b804f56330331d2
WORKER2=i-0519ba9c34b753140

echo "[*] Waiting for instances to be running..."
aws ec2 wait instance-running --instance-ids $MASTER $WORKER1 $WORKER2
echo "[+] All 3 running"

echo "[*] Allocating EIP for master"
ALLOC=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
aws ec2 associate-address --instance-id $MASTER --allocation-id "$ALLOC" --query 'AssociationId' --output text

echo ""
echo "=== Cluster state ==="
aws ec2 describe-instances --filters "Name=tag:Project,Values=elastikube-test" \
    --query 'Reservations[].Instances[].{Name:Tags[?Key==`Name`]|[0].Value,Id:InstanceId,State:State.Name,PublicIP:PublicIpAddress,PrivateIP:PrivateIpAddress}' \
    --output table
