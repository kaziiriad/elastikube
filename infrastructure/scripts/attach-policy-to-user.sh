#!/bin/bash
# Attach project policy to an existing IAM user
# Usage: ./attach-policy-to-user.sh <username>

set -e

USER_NAME="${1:-k3s-temp-user}"
REGION="ap-southeast-1"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

# Validation
if [ -z "$USER_NAME" ]; then
    echo "Usage: $0 <username>"
    exit 1
fi

log_info "Attaching policy to IAM user: $USER_NAME"

# Get AWS account ID for resource ARNs
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Create policy document
cat > /tmp/temp-user-policy.json <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "IAMRoleManagement",
            "Effect": "Allow",
            "Action": [
                "iam:CreateRole",
                "iam:DeleteRole",
                "iam:GetRole",
                "iam:UpdateRole",
                "iam:UpdateRoleDescription",
                "iam:TagRole",
                "iam:UntagRole",
                "iam:ListRoles"
            ],
            "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/k3s-*",
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": "ap-southeast-1"
                }
            }
        },
        {
            "Sid": "IAMInstanceProfileManagement",
            "Effect": "Allow",
            "Action": [
                "iam:CreateInstanceProfile",
                "iam:DeleteInstanceProfile",
                "iam:GetInstanceProfile",
                "iam:AddRoleToInstanceProfile",
                "iam:RemoveRoleFromInstanceProfile",
                "iam:ListInstanceProfilesForRole",
                "iam:TagInstanceProfile",
                "iam:UntagInstanceProfile"
            ],
            "Resource": "arn:aws:iam::${ACCOUNT_ID}:instance-profile/k3s-*",
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": "ap-southeast-1"
                }
            }
        },
        {
            "Sid": "IAMInlinePolicyManagement",
            "Effect": "Allow",
            "Action": [
                "iam:PutRolePolicy",
                "iam:DeleteRolePolicy",
                "iam:GetRolePolicy",
                "iam:ListRolePolicies"
            ],
            "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/k3s-*",
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": "ap-southeast-1"
                }
            }
        },
        {
            "Sid": "IAMManagedPolicyAttachment",
            "Effect": "Allow",
            "Action": [
                "iam:AttachRolePolicy",
                "iam:DetachRolePolicy",
                "iam:ListAttachedRolePolicies"
            ],
            "Resource": [
                "arn:aws:iam::${ACCOUNT_ID}:role/k3s-*",
                "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
            ],
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": "ap-southeast-1"
                }
            }
        },
        {
            "Sid": "IAMPassRole",
            "Effect": "Allow",
            "Action": [
                "iam:PassRole"
            ],
            "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/k3s-*",
            "Condition": {
                "StringEquals": {
                    "iam:PassedToService": [
                        "ec2.amazonaws.com",
                        "lambda.amazonaws.com"
                    ]
                }
            }
        },
        {
            "Sid": "IAMReadOnlyForDiscovery",
            "Effect": "Allow",
            "Action": [
                "iam:ListPolicies",
                "iam:ListPolicyVersions",
                "iam:GetPolicy",
                "iam:GetPolicyVersion"
            ],
            "Resource": "*"
        },
        {
            "Sid": "SSMGlobalActions",
            "Effect": "Allow",
            "Action": [
                "ssm:DescribeParameters"
            ],
            "Resource": "*"
        },
        {
            "Sid": "SSMParameterActions",
            "Effect": "Allow",
            "Action": [
                "ssm:GetParameters",
                "ssm:GetParameter",
                "ssm:GetParametersByPath"
            ],
            "Resource": "arn:aws:ssm:*:*:parameter/*"
        },
        {
            "Sid": "SecretsManagerGlobalActions",
            "Effect": "Allow",
            "Action": [
                "secretsmanager:DescribeSecret",
                "secretsmanager:GetSecretValue",
                "secretsmanager:GetResourcePolicy",
                "secretsmanager:ListSecrets"
            ],
            "Resource": "arn:aws:secretsmanager:*:*:secret:*"
        },
        {
            "Sid": "RegionalServices",
            "Effect": "Allow",
            "Action": [
                "ec2:*",
                "dynamodb:*",
                "events:*",
                "cloudwatch:*",
                "lambda:*",
                "logs:*",
                "autoscaling:*",
                "elasticloadbalancing:*",
                "apigateway:*",
                "ecr:*",
                "sqs:*",
                "sns:*",
                "states:*",
                "cloudformation:*",
                "vpc-lattice:*",
                "ssm:PutParameter",
                "ssm:DeleteParameter",
                "ssm:AddTagsToResource",
                "ssm:ListTagsForResource",
                "secretsmanager:CreateSecret",
                "secretsmanager:DeleteSecret",
                "secretsmanager:RestoreSecret",
                "secretsmanager:TagResource"
            ],
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": "ap-southeast-1"
                }
            }
        }
    ]
}
EOF

# Attach policy to user
log_info "Attaching ProjectAccessPolicy to $USER_NAME..."
aws iam put-user-policy \
    --user-name "$USER_NAME" \
    --policy-name ProjectAccessPolicy \
    --policy-document file:///tmp/temp-user-policy.json \
    --region "$REGION"

log_info "✓ Policy successfully attached to $USER_NAME"
echo ""
echo "Policy grants permissions for:"
echo "  - IAM (global)"
echo "  - EC2, DynamoDB, Lambda, CloudWatch, Events, Logs"
echo "  - SSM Parameter Store, Secrets Manager"
echo "  - (regional: ap-southeast-1 only)"
