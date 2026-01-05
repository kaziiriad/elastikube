#!/bin/bash
# Create a temporary IAM user for K3s Autoscaler project
# Set an alarm to delete after 5 hours!
# Usage: ./create-temp-user-simple.sh [username]

set -e

USER_NAME="${1:-k3s-autoscaler-temp}"
REGION="ap-southeast-1"
PROJECT_NAME="K3s Autoscaler"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║        Temporary IAM User Creator for $PROJECT_NAME         ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

log_info "Creating temporary IAM user: $USER_NAME"
log_warn "⚠️  Remember to DELETE this user after 5 hours to avoid charges!"
echo ""

# Check if user already exists
if aws iam get-user --user-name "$USER_NAME" 2>/dev/null; then
    log_error "User '$USER_NAME' already exists!"
    echo "To delete: aws iam delete-user --user-name $USER_NAME"
    exit 1
fi

# 1. Create IAM User
log_info "Step 1/3: Creating IAM user..."
aws iam create-user --user-name "$USER_NAME"

# 2. Create and attach policy
log_info "Step 2/3: Attaching project permissions policy..."

cat > /tmp/$USER_NAME-policy.json <<'EOF'
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ProjectResources",
            "Effect": "Allow",
            "Action": [
                "ec2:*",
                "dynamodb:*",
                "events:*",
                "cloudwatch:*",
                "lambda:*",
                "s3:*",
                "logs:*",
                "iam:CreateServiceLinkedRole",
                "iam:CreateRole",
                "iam:DeleteRole",
                "iam:AttachRolePolicy",
                "iam:PutRolePolicy",
                "iam:PassRole",
                "iam:TagRole",
                "iam:CreateInstanceProfile",
                "iam:DeleteInstanceProfile",
                "iam:AddRoleToInstanceProfile",
                "iam:RemoveRoleFromInstanceProfile",
                "iam:GetRole",
                "iam:GetInstanceProfile",
                "iam:ListRoles",
                "iam:ListInstanceProfiles",
                "autoscaling:*",
                "elasticloadbalancing:*",
                "cloudformation:*"
            ],
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": "ap-southeast-1"
                }
            }
        },
        {
            "Sid": "DenyInstanceTypes",
            "Effect": "Deny",
            "Action": "ec2:RunInstances",
            "Resource": "arn:aws:ec2:ap-southeast-1:*:instance/*",
            "Condition": {
                "ForAnyValue:StringNotLike": {
                    "ec2:InstanceType": ["t2.micro", "t2.small", "t3.small", "t3.medium"]
                }
            }
        },
        {
            "Sid": "DenyUserCreation",
            "Effect": "Deny",
            "Action": [
                "iam:CreateUser",
                "iam:CreateAccessKey",
                "iam:CreateLoginProfile"
            ],
            "Resource": "*"
        }
    ]
}
EOF

aws iam put-user-policy --user-name "$USER_NAME" --policy-name ProjectPolicy --policy-document file:///tmp/$USER_NAME-policy.json

# 3. Create access key
log_info "Step 3/3: Creating access key..."
CREDENTIALS=$(aws iam create-access-key --user-name "$USER_NAME" --query 'AccessKey' --output json)

ACCESS_KEY_ID=$(echo "$CREDENTIALS" | jq -r '.AccessKeyId')
SECRET_ACCESS_KEY=$(echo "$CREDENTIALS" | jq -r '.SecretAccessKey')

# Calculate deletion time
DELETION_TIME=$(date -u -d "+5 hours" +"%Y-%m-%d %H:%M:%S UTC")

# Success!
echo ""
log_info "══════════════════════════════════════════════════════════"
log_info "TEMPORARY IAM USER CREATED SUCCESSFULLY"
log_info "══════════════════════════════════════════════════════════"
echo ""
echo -e "${GREEN}📋 User Details:${NC}"
echo "  User Name:        $USER_NAME"
echo "  Region:           $REGION"
echo "  Account:          $(aws sts get-caller-identity --query 'Account' --output text)"
echo ""
echo -e "${GREEN}🔑 Credentials (SAVE THESE NOW!):${NC}"
echo "  Access Key ID:    $ACCESS_KEY_ID"
echo "  Secret Key:       $SECRET_ACCESS_KEY"
echo ""
echo -e "${RED}⏰ AUTO-DELETE IN 5 HOURS!${NC}"
echo "  Set alarm for:    $DELETION_TIME"
echo ""
echo -e "${YELLOW}🧹 To delete manually:${NC}"
echo "  aws iam delete-user-policy --user-name $USER_NAME --policy-name ProjectPolicy"
echo "  aws iam delete-access-key --user-name $USER_NAME --access-key-id $ACCESS_KEY_ID"
echo "  aws iam delete-user --user-name $USER_NAME"
echo ""
echo -e "${BLUE}🚀 To use with this project:${NC}"
echo ""
echo "  # Configure AWS CLI"
echo "  aws configure --profile $USER_NAME"
echo "    # Access Key ID: $ACCESS_KEY_ID"
echo "    # Secret Key: $SECRET_ACCESS_KEY"
echo "    # Region: $REGION"
echo ""
echo "  # Then run deployment"
echo "  export AWS_PROFILE=$USER_NAME"
echo "  cd /mnt/e/custom_autoscaler/production/infrastructure/pulumi"
echo "  pulumi up"
echo ""

# Save credentials to file for reference
cat > /tmp/${USER_NAME}-credentials.txt <<EOF
K3s Autoscaler Temporary IAM User Credentials
=============================================

User Name: $USER_NAME
Created: $(date -u +"%Y-%m-%d %H:%M:%S UTC")
DELETE BEFORE: $DELETION_TIME

Access Key ID: $ACCESS_KEY_ID
Secret Access Key: $SECRET_ACCESS_KEY
Region: $REGION

To delete:
  aws iam delete-user-policy --user-name $USER_NAME --policy-name ProjectPolicy
  aws iam delete-access-key --user-name $USER_NAME --access-key-id $ACCESS_KEY_ID
  aws iam delete-user --user-name $USER_NAME
EOF

log_info "Credentials also saved to: /tmp/${USER_NAME}-credentials.txt"

# Clean up
rm -f /tmp/$USER_NAME-policy.json

echo ""
log_warn "⚠️  REMINDER: Delete this user within 5 hours to avoid AWS charges!"
echo "   Free tier limits: 750 hours/month t2.micro"
echo ""
