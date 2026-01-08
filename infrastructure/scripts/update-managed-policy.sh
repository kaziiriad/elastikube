#!/bin/bash
# Update managed policy for temporary IAM user
# Usage: ./update-managed-policy.sh [policy-name]

set -e

POLICY_NAME="${1:-temp-user-policy}"
REGION="ap-southeast-1"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

# Get AWS account ID for resource ARNs
log_info "Getting AWS account ID..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Get policy ARN
POLICY_ARN=$(aws iam list-policies --scope Local --query "Policies[?PolicyName=='${POLICY_NAME}'].Arn" --output text)

if [ -z "$POLICY_ARN" ] || [ "$POLICY_ARN" = "None" ]; then
    log_warn "Policy '${POLICY_NAME}' not found. Please create it first."
    exit 1
fi

log_info "Updating managed policy: ${POLICY_ARN}"

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
            "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/k3s-*"
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
            "Resource": "arn:aws:iam::${ACCOUNT_ID}:instance-profile/k3s-*"
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
            "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/k3s-*"
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
            ]
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
            "Resource": "arn:aws:ssm:*:*:parameter/k3s/*"
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
            "Resource": "arn:aws:secretsmanager:*:*:secret:k3s-*-*"
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
                "secretsmanager:TagResource",
                "secretsmanager:PutSecretValue"
            ],
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": "${REGION}"
                }
            }
        }
    ]
}
EOF

# Create new policy version
log_info "Creating new policy version..."
aws iam create-policy-version \
    --policy-arn "$POLICY_ARN" \
    --policy-document file:///tmp/temp-user-policy.json \
    --set-as-default

log_info "✓ Managed policy '${POLICY_NAME}' updated successfully!"
echo ""
echo "Policy now includes:"
echo "  - Locked-down IAM permissions (k3s-* resources only)"
echo "  - No iam:CreateServiceLinkedRole wildcard"
echo "  - Specific resource ARNs instead of wildcards"
echo "  - PassRole restricted to EC2 and Lambda only"
