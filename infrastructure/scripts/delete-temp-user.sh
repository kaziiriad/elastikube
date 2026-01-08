#!/bin/bash
# Delete a time-limited IAM user and all associated resources
# Usage: ./delete-temp-user.sh <username> [--force] [--debug]

# Don't exit on errors - handle them manually
set +e

# Enable debug mode if --debug flag is set
if [[ "$*" == *"--debug"* ]]; then
    set -x
    echo "Debug mode enabled"
fi

USER_NAME="${1}"
REGION="${AWS_REGION:-ap-southeast-1}"
FORCE="${2:-false}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${CYAN}[STEP]${NC} $1"; }

# Validation
if [ -z "$USER_NAME" ]; then
    log_error "Usage: $0 <username> [--force]"
    echo ""
    echo "Arguments:"
    echo "  username    Name of the IAM user to delete"
    echo "  --force     Skip confirmation prompt"
    exit 1
fi

# Check AWS credentials
log_step "Checking AWS credentials..."
if ! aws sts get-caller-identity &>/dev/null; then
    log_error "AWS credentials not configured or invalid!"
    echo ""
    echo "Please configure AWS credentials first:"
    echo "  aws configure --profile <your-profile>"
    echo "  export AWS_PROFILE=<your-profile>"
    echo ""
    echo "Or set environment variables:"
    echo "  export AWS_ACCESS_KEY_ID=..."
    echo "  export AWS_SECRET_ACCESS_KEY=..."
    exit 1
fi

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=$(aws configure get region || echo "$REGION")
log_info "✓ AWS credentials OK (Account: $AWS_ACCOUNT_ID, Region: ${AWS_REGION:-$REGION})"

# Confirmation (unless --force flag is set)
if [ "$FORCE" != "--force" ]; then
    echo ""
    log_warn "You are about to delete the IAM user: $USER_NAME"
    echo ""
    echo "This will permanently delete:"
    echo "  - IAM user and all access keys"
    echo "  - All user policies"
    echo "  - Lambda cleanup function"
    echo "  - EventBridge scheduled rule"
    echo "  - Lambda IAM role"
    echo ""
    read -p "$(echo -e ${YELLOW}Are you sure? [y/N]: ${NC})" -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Aborted."
        exit 0
    fi
fi

echo ""
log_info "=========================================="
log_info "DELETING IAM USER: $USER_NAME"
log_info "=========================================="
echo ""

DELETED_COUNT=0
SKIPPED_COUNT=0

# Helper function with better error handling
delete_resource() {
    local resource_type="$1"
    local resource_name="$2"
    local delete_cmd="$3"

    # Add timeout to prevent hanging
    if timeout 30 bash -c "$delete_cmd" 2>/dev/null; then
        log_info "✓ Deleted $resource_type: $resource_name"
        ((DELETED_COUNT++))
        return 0
    else
        log_warn "⊘ Skipped $resource_type: $resource_name (not found or timeout)"
        ((SKIPPED_COUNT++))
        return 0
    fi
}

# AWS options for all commands
AWS_OPTS="--region $REGION --cli-read-timeout 30 --cli-connect-timeout 10"

# 1. Delete access keys
log_step "1/8: Deleting access keys..."
ACCESS_KEYS=$(aws iam list-access-keys --user-name "$USER_NAME" --query 'AccessKeyMetadata[].AccessKeyId' --output text $AWS_OPTS 2>/dev/null || echo "")
if [ -n "$ACCESS_KEYS" ]; then
    for key_id in $ACCESS_KEYS; do
        delete_resource "access key" "$key_id" "aws iam delete-access-key --user-name '$USER_NAME' --access-key-id '$key_id' $AWS_OPTS"
    done
else
    log_warn "No access keys found"
fi

# 2. Delete user policies
log_step "2/8: Deleting user policies..."
POLICIES=$(aws iam list-user-policies --user-name "$USER_NAME" --query 'PolicyNames[]' --output text $AWS_OPTS 2>/dev/null || echo "")
if [ -n "$POLICIES" ]; then
    for policy in $POLICIES; do
        delete_resource "user policy" "$policy" "aws iam delete-user-policy --user-name '$USER_NAME' --policy-name '$policy' $AWS_OPTS"
    done
else
    log_warn "No user policies found"
fi

# 3. Remove from groups
log_step "3/8: Removing from groups..."
GROUPS=$(aws iam list-groups-for-user --user-name "$USER_NAME" --query 'Groups[].GroupName' --output text $AWS_OPTS 2>/dev/null || echo "")
if [ -n "$GROUPS" ]; then
    for group in $GROUPS; do
        delete_resource "group membership" "$group" "aws iam remove-user-from-group --group-name '$group' --user-name '$USER_NAME' $AWS_OPTS"
    done
else
    log_warn "No group memberships found"
fi

# 4. Delete Lambda function
log_step "4/8: Deleting Lambda cleanup function..."
LAMBDA_FUNCTION_NAME="${USER_NAME}-cleanup"
LAMBDA_EXISTS=$(timeout 10 aws lambda get-function --function-name "$LAMBDA_FUNCTION_NAME" $AWS_OPTS &>/dev/null && echo "yes" || echo "no")
if [ "$LAMBDA_EXISTS" = "yes" ]; then
    delete_resource "Lambda function" "$LAMBDA_FUNCTION_NAME" "aws lambda delete-function --function-name '$LAMBDA_FUNCTION_NAME' $AWS_OPTS"
else
    log_warn "Lambda function not found or timeout: $LAMBDA_FUNCTION_NAME"
    ((SKIPPED_COUNT++))
fi

# 5. Delete EventBridge rule
log_step "5/8: Deleting EventBridge rule..."
RULE_NAME="${USER_NAME}-cleanup-rule"
EVENT_EXISTS=$(timeout 10 aws events describe-rule --name "$RULE_NAME" $AWS_OPTS &>/dev/null && echo "yes" || echo "no")
if [ "$EVENT_EXISTS" = "yes" ]; then
    # Remove targets first
    aws events remove-targets --rule "$RULE_NAME" --ids 1 $AWS_OPTS 2>/dev/null || true
    delete_resource "EventBridge rule" "$RULE_NAME" "aws events delete-rule --name '$RULE_NAME' $AWS_OPTS"
else
    log_warn "EventBridge rule not found or timeout: $RULE_NAME"
    ((SKIPPED_COUNT++))
fi

# 6. Delete Lambda role policies
log_step "6/8: Deleting Lambda role policies..."
LAMBDA_ROLE_NAME="${USER_NAME}-cleanup-role"
ROLE_EXISTS=$(timeout 10 aws iam get-role --role-name "$LAMBDA_ROLE_NAME" $AWS_OPTS &>/dev/null && echo "yes" || echo "no")
if [ "$ROLE_EXISTS" = "yes" ]; then
    # Detach managed policies
    aws iam detach-role-policy \
        --role-name "$LAMBDA_ROLE_NAME" \
        --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" \
        $AWS_OPTS 2>/dev/null || true

    # Delete inline policies
    ROLE_POLICIES=$(aws iam list-role-policies --role-name "$LAMBDA_ROLE_NAME" --query 'PolicyNames[]' --output text $AWS_OPTS 2>/dev/null || echo "")
    if [ -n "$ROLE_POLICIES" ]; then
        for policy in $ROLE_POLICIES; do
            delete_resource "role policy" "$policy" "aws iam delete-role-policy --role-name '$LAMBDA_ROLE_NAME' --policy-name '$policy' $AWS_OPTS"
        done
    fi
else
    log_warn "Lambda role not found: $LAMBDA_ROLE_NAME"
    ((SKIPPED_COUNT++))
fi

# 7. Delete Lambda IAM role
log_step "7/8: Deleting Lambda IAM role..."
delete_resource "IAM role" "$LAMBDA_ROLE_NAME" "timeout 30 aws iam delete-role --role-name '$LAMBDA_ROLE_NAME' $AWS_OPTS"

# 8. Delete IAM user
log_step "8/8: Deleting IAM user..."
USER_EXISTS=$(timeout 10 aws iam get-user --user-name "$USER_NAME" $AWS_OPTS &>/dev/null && echo "yes" || echo "no")
if [ "$USER_EXISTS" = "yes" ]; then
    delete_resource "IAM user" "$USER_NAME" "timeout 30 aws iam delete-user --user-name '$USER_NAME' $AWS_OPTS"
else
    log_warn "IAM user not found: $USER_NAME"
    ((SKIPPED_COUNT++))
fi

# Clean up local credential files
log_step "Cleaning up local files..."
LOCAL_CREDS="./${USER_NAME}-credentials.sh"
if [ -f "$LOCAL_CREDS" ]; then
    rm -f "$LOCAL_CREDS"
    log_info "✓ Deleted: $LOCAL_CREDS"
    ((DELETED_COUNT++))
else
    log_warn "No local credential file found"
fi

# Summary
echo ""
log_info "=========================================="
log_info "DELETION COMPLETE"
log_info "=========================================="
echo ""
echo -e "${GREEN}✓ Deleted:${NC}   $DELETED_COUNT resources"
echo -e "${YELLOW}⊘ Skipped:${NC}    $SKIPPED_COUNT resources (not found)"
echo ""

if [ $DELETED_COUNT -eq 0 ] && [ $SKIPPED_COUNT -gt 0 ]; then
    log_warn "No resources were deleted - user may not exist"
elif aws iam get-user --user-name "$USER_NAME" &>/dev/null; then
    log_error "User still exists - manual cleanup may be required"
else
    log_info "✓ User $USER_NAME and all resources successfully deleted"
fi
