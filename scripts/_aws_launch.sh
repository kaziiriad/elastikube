#!/bin/bash
# Launch 3 t3.small EC2 instances for the 1+2 k3s test cluster:
#   k3s-master  — public subnet, EIP, control plane
#   k3s-worker-1 — private subnet, no EIP
#   k3s-worker-2 — private subnet, no EIP
# All attach to k3s-worker IAM instance profile. Idempotent.

set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1
REGION=ap-southeast-1

# Source the network IDs (set during provisioning)
source <(grep -E '^(VPC_ID|PUBLIC_SUBNET_ID|PRIVATE_SUBNET_ID|SG_ID|KEY_NAME|AMI_ID|INSTANCE_TYPE)=' /mnt/e/custom_autoscaler/production/scripts/elastikube-test-state.env)

echo "============================================================"
echo "  Launching 3× t3.small EC2 instances"
echo "============================================================"

launch_node() {
    local name=$1
    local subnet_id=$2
    local role=$3
    local ami_subnet=$(echo "$subnet_id" | cut -c1-3)  # for log grouping

    # Check if already running
    EXISTING=$(aws ec2 describe-instances \
        --filters "Name=tag:Name,Values=$name" "Name=instance-state.name,Values=running,pending,stopped" \
        --query 'Reservations[].Instances[0].[InstanceId,State.Name,PrivateIpAddress]' --output text 2>/dev/null)
    if [ -n "$EXISTING" ] && ! echo "$EXISTING" | grep -q "None"; then
        echo "[=] $name already running: $EXISTING"
        echo "$EXISTING" | awk -v n="$name" '{print n"|"$1"|"$2"|"$3}' >> /tmp/ec2-ids
        return
    fi

    echo "[+] Launching $name in subnet $subnet_id (role=$role)"
    INSTANCE_ID=$(aws ec2 run-instances \
        --image-id "$AMI_ID" \
        --instance-type "$INSTANCE_TYPE" \
        --subnet-id "$subnet_id" \
        --security-group-ids "$SG_ID" \
        --iam-instance-profile Name=k3s-worker \
        --associate-public-ip-address $([ "$subnet_id" = "$PUBLIC_SUBNET_ID" ] && echo true || echo false) \
        --key-name "$KEY_NAME" \
        --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3"}}]' \
        --tag-specifications \
            "ResourceType=instance,Tags=[{Key=Name,Value=$name},{Key=Role,Value=$role},{Key=Permanent,Value=true},{Key=Cluster,Value=production-k3s},{Key=Project,Value=elastikube-test}]" \
        --query 'Instances[0].InstanceId' --output text)
    echo "    Created: $INSTANCE_ID"
    echo "$name|$INSTANCE_ID" >> /tmp/ec2-ids
}

rm -f /tmp/ec2-ids
launch_node k3s-master  "$PUBLIC_SUBNET_ID"  k3s-master
launch_node k3s-worker-1 "$PRIVATE_SUBNET_ID" k3s-worker
launch_node k3s-worker-2 "$PRIVATE_SUBNET_ID" k3s-worker

echo ""
echo "[*] Waiting for all 3 instances to be running..."
aws ec2 wait instance-running --region "$REGION" \
    --instance-ids $(awk -F'|' '{print $2}' /tmp/ec2-ids)

# Allocate + attach an EIP to k3s-master
MASTER_ID=$(grep '^k3s-master|' /tmp/ec2-ids | awk -F'|' '{print $2}')
if [ -n "$MASTER_ID" ]; then
    ALLOC=$(aws ec2 describe-addresses \
        --filters "Name=instance-id,Values=$MASTER_ID" \
        --query 'Addresses[0].AllocationId' --output text 2>/dev/null)
    if [ -z "$ALLOC" ] || [ "$ALLOC" = "None" ]; then
        echo "[+] Allocating EIP for k3s-master"
        ALLOC=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
        aws ec2 associate-address --instance-id "$MASTER_ID" --allocation-id "$ALLOC"
    fi
fi

# Refresh the instance list so we have IPs
> /tmp/ec2-ids
aws ec2 describe-instances \
    --filters "Name=tag:Project,Values=elastikube-test" "Name=instance-state.name,Values=running" \
    --query 'Reservations[].Instances[].{Name:Tags[?Key==`Name`]|[0].Value,Id:InstanceId,PublicIP:PublicIpAddress,PrivateIP:PrivateIpAddress}' \
    --output text >> /tmp/ec2-ids.tmp
# Normalize the multi-line output
awk -F'\t' '{print $1"|"$2"|"$3"|"$4}' /tmp/ec2-ids.tmp > /tmp/ec2-ids
rm -f /tmp/ec2-ids.tmp

echo ""
echo "============================================================"
echo "  Cluster instances running"
echo "============================================================"
cat /tmp/ec2-ids
