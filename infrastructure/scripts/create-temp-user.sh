#!/bin/bash
# Create a time-limited IAM user that auto-deletes after 5 hours
# Usage: ./create-temp-user.sh <username>

set -e

USER_NAME="${1:-k3s-autoscaler-temp}"
DURATION_HOURS=5
REGION="ap-southeast-1"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

log_info "Creating time-limited IAM user: $USER_NAME"
log_info "This user will be automatically deleted after $DURATION_HOURS hours"
echo ""

# Calculate deletion time (5 hours from now in UTC)
DELETION_TIME=$(date -u -d "+$DURATION_HOURS hours" +"%Y-%m-%dT%H:%M:%S")
log_info "Scheduled deletion time: $DELETION_TIME (UTC)"
echo ""

# 1. Create IAM User
log_info "Step 1/5: Creating IAM user..."
aws iam create-user --user-name "$USER_NAME" || log_warn "User might already exist"

# 2. Create policy with project permissions
log_info "Step 2/5: Creating inline policy..."

cat > /tmp/temp-user-policy.json <<'EOF'
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "IAMGlobalActions",
            "Effect": "Allow",
            "Action": [
                "iam:*"
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

aws iam put-user-policy --user-name "$USER_NAME" --policy-name ProjectAccessPolicy --policy-document file:///tmp/temp-user-policy.json

# 3. Create access key
log_info "Step 3/5: Creating access key..."
CREDENTIALS=$(aws iam create-access-key --user-name "$USER_NAME" --query 'AccessKey' --output json)

ACCESS_KEY_ID=$(echo "$CREDENTIALS" | jq -r '.AccessKeyId')
SECRET_ACCESS_KEY=$(echo "$CREDENTIALS" | jq -r '.SecretAccessKey')

# 4. Create auto-delete Lambda function
log_info "Step 4/5: Creating auto-delete Lambda function..."

cat > /tmp/delete-user-lambda.py <<'EOF'
import json
import boto3
import os

def lambda_handler(event, context):
    user_name = os.environ['TARGET_USER']
    iam = boto3.client('iam')

    try:
        # Delete access keys first
        keys = iam.list_access_keys(UserName=user_name)
        for key in keys['AccessKeyMetadata']:
            iam.delete_access_key(UserName=user_name, AccessKeyId=key['AccessKeyId'])

        # Delete user policies
        policies = iam.list_user_policies(UserName=user_name)
        for policy in policies['PolicyNames']:
            iam.delete_user_policy(UserName=user_name, PolicyName=policy)

        # Delete user
        iam.delete_user(UserName=user_name)

        return {
            'statusCode': 200,
            'body': json.dumps(f'Successfully deleted IAM user: {user_name}')
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps(f'Error: {str(e)}')
        }
EOF

# Create Lambda zip
zip -j /tmp/delete-user-function.zip /tmp/delete-user-lambda.py

# Create Lambda IAM role
cat > /tmp/lambda-trust-policy.json <<'EOF'
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}
EOF

LAMBDA_ROLE_NAME="${USER_NAME}-cleanup-role"

# Create or get Lambda role
if aws iam get-role --role-name "$LAMBDA_ROLE_NAME" 2>/dev/null; then
    log_warn "Lambda role already exists"
else
    aws iam create-role --role-name "$LAMBDA_ROLE_NAME" --assume-role-policy-document file:///tmp/lambda-trust-policy.json
    aws iam attach-role-policy --role-name "$LAMBDA_ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    # Create policy document with proper quoting
    cat > /tmp/lambda-iam-policy.json <<EOF
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": ["iam:DeleteAccessKey", "iam:DeleteUserPolicy", "iam:ListAccessKeys", "iam:ListUserPolicies", "iam:DeleteUser"],
        "Resource": ["arn:aws:iam::*:user/${USER_NAME}"]
    }]
}
EOF

    aws iam put-role-policy --role-name "$LAMBDA_ROLE_NAME" --policy-name IAMDeleteUserPolicy --policy-document file:///tmp/lambda-iam-policy.json
fi

LAMBDA_ROLE_ARN=$(aws iam get-role --role-name "$LAMBDA_ROLE_NAME" --query 'Role.Arn' --output text)

# Sleep to let role propagate
sleep 10

# Create Lambda function
LAMBDA_FUNCTION_NAME="${USER_NAME}-cleanup"
aws lambda create-function \
    --function-name "$LAMBDA_FUNCTION_NAME" \
    --runtime "python3.11" \
    --role "$LAMBDA_ROLE_ARN" \
    --handler "delete-user-lambda.lambda_handler" \
    --zip-file fileb:///tmp/delete-user-function.zip \
    --environment Variables="{TARGET_USER=$USER_NAME}" \
    --timeout 30 \
    --region "$REGION" 2>/dev/null || aws lambda update-function-code --function-name "$LAMBDA_FUNCTION_NAME" --zip-file fileb:///tmp/delete-user-function.zip --region "$REGION"

# 5. Schedule EventBridge rule
log_info "Step 5/5: Scheduling auto-deletion..."

# Convert deletion time to cron format for EventBridge
MINUTE=$(date -u -d "+$DURATION_HOURS hours" +"%M")
HOUR=$(date -u -d "+$DURATION_HOURS hours" +"%H")
DAY=$(date -u -d "+$DURATION_HOURS hours" +"%d")
MONTH=$(date -u -d "+$DURATION_HOURS hours" +"%m")
YEAR=$(date -u -d "+$DURATION_HOURS hours" +"%Y")

RULE_NAME="${USER_NAME}-cleanup-rule"

# Create EventBridge rule
aws events put-rule \
    --name "$RULE_NAME" \
    --schedule-expression "cron($MINUTE $HOUR $DAY $MONTH ? $YEAR)" \
    --region "$REGION"

# Add Lambda target
aws lambda add-permission \
    --function-name "$LAMBDA_FUNCTION_NAME" \
    --statement-id "$RULE_NAME" \
    --action "lambda:InvokeFunction" \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:$REGION:$(aws sts get-caller-identity --query Account --output text):rule/$RULE_NAME" \
    --region "$REGION"

aws events put-targets \
    --rule "$RULE_NAME" \
    --targets "Id=1,Arn=$(aws lambda get-function --function-name $LAMBDA_FUNCTION_NAME --query 'Configuration.FunctionArn' --output text --region $REGION)" \
    --region "$REGION"

# Success!
echo ""
log_info "=========================================="
log_info "TIME-LIMITED IAM USER CREATED SUCCESSFULLY"
log_info "=========================================="
echo ""
echo -e "${GREEN}User Name:${NC}     $USER_NAME"
echo -e "${GREEN}Access Key ID:${NC}  $ACCESS_KEY_ID"
echo -e "${GREEN}Secret Key:${NC}     $SECRET_ACCESS_KEY"
echo -e "${GREEN}Region:${NC}         $REGION"
echo ""
echo -e "${YELLOW}⏰ This user will be automatically deleted at:${NC}"
echo -e "   $DELETION_TIME (UTC)"
echo ""
echo -e "${YELLOW}⚠️  SAVE THESE CREDENTIALS NOW - they won't be shown again!${NC}"
echo ""
echo "To configure AWS CLI with these credentials:"
echo ""
echo -e "${GREEN}aws configure --profile $USER_NAME${NC}"
echo "  AWS Access Key ID: $ACCESS_KEY_ID"
echo "  AWS Secret Access Key: $SECRET_ACCESS_KEY"
echo "  Default region name: $REGION"
echo "  Default output format: json"
echo ""
echo "Then use with: export AWS_PROFILE=$USER_NAME"
echo ""
echo -e "${YELLOW}NOTE: After $DURATION_HOURS hours, the user and all credentials will be permanently deleted.${NC}"

# Export credentials to a file that can be sourced
EXPORT_FILE="./${USER_NAME}-credentials.sh"
TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M:%S UTC")

cat > "$EXPORT_FILE" <<EOF
# AWS Credentials for $USER_NAME
# Generated: ${TIMESTAMP}
# Auto-deletes: ${DELETION_TIME} (UTC)

export AWS_ACCESS_KEY_ID="${ACCESS_KEY_ID}"
export AWS_SECRET_ACCESS_KEY="${SECRET_ACCESS_KEY}"
export AWS_DEFAULT_REGION="${REGION}"
export AWS_PROFILE="${USER_NAME}"

# To use these credentials:
# 1. Source this file: source ${USER_NAME}-credentials.sh
# 2. Or run: . ${USER_NAME}-credentials.sh
# 3. To unset: unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION AWS_PROFILE
EOF

chmod 600 "$EXPORT_FILE"
echo ""
echo -e "${GREEN}✓ Credentials exported to:${NC} $EXPORT_FILE"
echo -e "  To use: source $EXPORT_FILE"
echo -e "  Or: . $EXPORT_FILE"
