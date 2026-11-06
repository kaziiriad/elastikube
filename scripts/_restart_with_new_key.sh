#!/bin/bash
# Terminate the existing 3 elastikube-test EC2s and re-launch them with the
# freshly-regenerated SSH key. Also re-creates the S3 bootstrap bucket and
# (if missing) the EIP.

set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1
KEY=/tmp/elastikube-test.pem

# --- Terminate existing 3 EC2s ---
echo "[*] Terminating existing elastikube-test instances"
INSTANCES=$(aws ec2 describe-instances \
    --filters "Name=tag:Project,Values=elastikube-test" \
    --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text \
    | awk '$2 == "running" || $2 == "pending" {print $1}')
echo "    Targets: $INSTANCES"
if [ -n "$INSTANCES" ]; then
    aws ec2 terminate-instances --instance-ids $INSTANCES >/dev/null
    aws ec2 wait instance-terminated --instance-ids $INSTANCES
    echo "[+] All instances terminated"
fi

# --- Release the old EIP (it stays allocated when the instance dies) ---
echo "[*] Releasing old EIP"
ALLOC=$(aws ec2 describe-addresses \
    --query 'Addresses[].{Id:AllocationId,IP:PublicIp}' --output text 2>/dev/null | head -1)
if [ -n "$ALLOC" ]; then
    ALLOC_ID=$(echo "$ALLOC" | awk '{print $1}')
    aws ec2 release-address --allocation-id "$ALLOC_ID" >/dev/null 2>&1 || true
    echo "[+] Released $ALLOC_ID"
fi

# --- Re-launch master ---
echo "[*] Re-launching k3s-master"
bash /mnt/e/custom_autoscaler/production/scripts/_launch_one.sh | head -10

# --- Re-launch workers ---
echo "[*] Re-launching workers"
bash /mnt/e/custom_autoscaler/production/scripts/_launch_workers.sh | head -20

# --- Wait + EIP ---
echo "[*] Waiting for instances + allocating EIP"
bash /mnt/e/custom_autoscaler/production/scripts/_wait_and_eip.sh | head -25

echo "[+] Cluster relaunched. Master public IP:"
bash /mnt/e/custom_autoscaler/production/scripts/_show_cluster.sh | head -10
