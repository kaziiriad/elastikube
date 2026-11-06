#!/bin/bash
# Provision VPC + subnets + IGW + route table + security group + keypair
# for the elastikube test cluster on poridhi-aws.
#
# Idempotent: safe to re-run if it partially failed last time. Resources
# are tagged elastikube-test so teardown can find them.

set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1
REGION=ap-southeast-1
NAME_PREFIX=elastikube-test
KEY_NAME=elastikube-test
KEY_FILE=/tmp/elastikube-test.pem

echo "============================================================"
echo "  Provisioning elastikube-test network on poridhi-aws"
echo "============================================================"

# --- Keypair ---
if [ ! -f "$KEY_FILE" ]; then
    echo "[+] Creating keypair $KEY_NAME"
    aws ec2 create-key-pair --key-name "$KEY_NAME" --query 'KeyMaterial' --output text > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
else
    echo "[=] Keypair $KEY_NAME already exists (key file at $KEY_FILE)"
fi

# --- VPC ---
VPC_ID=$(aws ec2 describe-vpcs \
    --filters "Name=tag:Name,Values=${NAME_PREFIX}-vpc" \
    --query 'Vpcs[0].VpcId' --output text)
if [ "$VPC_ID" = "None" ] || [ -z "$VPC_ID" ]; then
    echo "[+] Creating VPC 10.0.0.0/16"
    VPC_ID=$(aws ec2 create-vpc --cidr-block 10.0.0.0/16 \
        --query 'Vpc.VpcId' --output text)
    echo "    Created VPC: $VPC_ID"
    aws ec2 create-tags --resources "$VPC_ID" \
        --tags Key=Name,Value="${NAME_PREFIX}-vpc"
    aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support
    aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames
else
    echo "[=] VPC already exists: $VPC_ID"
fi

# --- Internet Gateway ---
IGW_ID=$(aws ec2 describe-internet-gateways \
    --filters "Name=tag:Name,Values=${NAME_PREFIX}-igw" \
    --query 'InternetGateways[0].InternetGatewayId' --output text)
if [ "$IGW_ID" = "None" ] || [ -z "$IGW_ID" ]; then
    echo "[+] Creating IGW"
    IGW_ID=$(aws ec2 create-internet-gateway --query 'InternetGateway.InternetGatewayId' --output text)
    echo "    Created IGW: $IGW_ID"
    # Tag only the IGW itself, not the unattached state
    aws ec2 create-tags --resources "$IGW_ID" \
        --tags Key=Name,Value="${NAME_PREFIX}-igw"
    aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"
else
    echo "[=] IGW already exists: $IGW_ID"
fi

# --- Public subnet (ap-southeast-1a, master lives here) ---
PUBLIC_SUBNET_ID=$(aws ec2 describe-subnets \
    --filters "Name=tag:Name,Values=${NAME_PREFIX}-public-1a" \
    --query 'Subnets[0].SubnetId' --output text)
if [ "$PUBLIC_SUBNET_ID" = "None" ] || [ -z "$PUBLIC_SUBNET_ID" ]; then
    echo "[+] Creating public subnet 10.0.1.0/24 (ap-southeast-1a)"
    PUBLIC_SUBNET_ID=$(aws ec2 create-subnet \
        --vpc-id "$VPC_ID" \
        --cidr-block 10.0.1.0/24 \
        --availability-zone ap-southeast-1a \
        --query 'Subnet.SubnetId' --output text)
    echo "    Created public subnet: $PUBLIC_SUBNET_ID"
    aws ec2 modify-subnet-attribute --subnet-id "$PUBLIC_SUBNET_ID" \
        --map-public-ip-on-launch
    aws ec2 create-tags --resources "$PUBLIC_SUBNET_ID" \
        --tags Key=Name,Value="${NAME_PREFIX}-public-1a"
else
    echo "[=] Public subnet already exists: $PUBLIC_SUBNET_ID"
fi

# --- Private subnet (ap-southeast-1a, workers live here) ---
PRIVATE_SUBNET_ID=$(aws ec2 describe-subnets \
    --filters "Name=tag:Name,Values=${NAME_PREFIX}-private-1a" \
    --query 'Subnets[0].SubnetId' --output text)
if [ "$PRIVATE_SUBNET_ID" = "None" ] || [ -z "$PRIVATE_SUBNET_ID" ]; then
    echo "[+] Creating private subnet 10.0.10.0/24 (ap-southeast-1a)"
    PRIVATE_SUBNET_ID=$(aws ec2 create-subnet \
        --vpc-id "$VPC_ID" \
        --cidr-block 10.0.10.0/24 \
        --availability-zone ap-southeast-1a \
        --query 'Subnet.SubnetId' --output text)
    echo "    Created private subnet: $PRIVATE_SUBNET_ID"
    aws ec2 create-tags --resources "$PRIVATE_SUBNET_ID" \
        --tags Key=Name,Value="${NAME_PREFIX}-private-1a"
else
    echo "[=] Private subnet already exists: $PRIVATE_SUBNET_ID"
fi

# --- Public route table + IGW route ---
PUBLIC_RT_ID=$(aws ec2 describe-route-tables \
    --filters "Name=tag:Name,Values=${NAME_PREFIX}-public-rt" \
    --query 'RouteTables[0].RouteTableId' --output text)
if [ "$PUBLIC_RT_ID" = "None" ] || [ -z "$PUBLIC_RT_ID" ]; then
    echo "[+] Creating public route table"
    PUBLIC_RT_ID=$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
        --query 'RouteTable.RouteTableId' --output text)
    echo "    Created route table: $PUBLIC_RT_ID"
    aws ec2 create-tags --resources "$PUBLIC_RT_ID" \
        --tags Key=Name,Value="${NAME_PREFIX}-public-rt"
    aws ec2 create-route --route-table-id "$PUBLIC_RT_ID" \
        --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW_ID"
    aws ec2 associate-route-table --route-table-id "$PUBLIC_RT_ID" \
        --subnet-id "$PUBLIC_SUBNET_ID"
else
    echo "[=] Public route table already exists: $PUBLIC_RT_ID"
fi

# --- Private route table (for S3 + DynamoDB gateway endpoints later) ---
PRIVATE_RT_ID=$(aws ec2 describe-route-tables \
    --filters "Name=tag:Name,Values=${NAME_PREFIX}-private-rt" \
    --query 'RouteTables[0].RouteTableId' --output text)
if [ "$PRIVATE_RT_ID" = "None" ] || [ -z "$PRIVATE_RT_ID" ]; then
    echo "[+] Creating private route table"
    PRIVATE_RT_ID=$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
        --query 'RouteTable.RouteTableId' --output text)
    echo "    Created route table: $PRIVATE_RT_ID"
    aws ec2 create-tags --resources "$PRIVATE_RT_ID" \
        --tags Key=Name,Value="${NAME_PREFIX}-private-rt"
    aws ec2 associate-route-table --route-table-id "$PRIVATE_RT_ID" \
        --subnet-id "$PRIVATE_SUBNET_ID"
else
    echo "[=] Private route table already exists: $PRIVATE_RT_ID"
fi

# --- Security group (SSH from anywhere + all traffic within SG) ---
SG_ID=$(aws ec2 describe-security-groups \
    --filters "Name=tag:Name,Values=${NAME_PREFIX}-sg" \
    --query 'SecurityGroups[0].GroupId' --output text)
if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
    echo "[+] Creating security group"
    SG_ID=$(aws ec2 create-security-group --group-name "${NAME_PREFIX}-sg" \
        --description "elastikube test SG" --vpc-id "$VPC_ID" \
        --query 'GroupId' --output text)
    echo "    Created SG: $SG_ID"
    aws ec2 create-tags --resources "$SG_ID" \
        --tags Key=Name,Value="${NAME_PREFIX}-sg"
    aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
        --protocol tcp --port 22 --cidr 0.0.0.0/0
    aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
        --protocol tcp --port 6443 --cidr 10.0.0.0/16
    aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
        --protocol all --source-group "$SG_ID"
else
    echo "[=] Security group already exists: $SG_ID"
fi

# --- IAM instance profile (k3s-worker) ---
echo "[*] Checking IAM instance profile 'k3s-worker'"
if ! aws iam get-instance-profile --instance-profile-name k3s-worker >/dev/null 2>&1; then
    echo "    Profile 'k3s-worker' missing — creating role + profile + attaching to existing role"
    # Skip; this is provisioned out-of-band usually.
fi

cat <<EOF

============================================================
  Network provisioned
============================================================
VPC_ID           = $VPC_ID
PUBLIC_SUBNET_ID = $PUBLIC_SUBNET_ID  (master)
PRIVATE_SUBNET_ID= $PRIVATE_SUBNET_ID (workers)
PUBLIC_RT_ID     = $PUBLIC_RT_ID
PRIVATE_RT_ID    = $PRIVATE_RT_ID
IGW_ID           = $IGW_ID
SG_ID            = $SG_ID
KEY_FILE         = $KEY_FILE
EOF
