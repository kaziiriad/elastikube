#!/bin/bash
# Update CloudWatch Dashboard with actual AWS resource names
# This script queries AWS for current resource names and updates the dashboard

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_TEMPLATE="${SCRIPT_DIR}/../dashboards/k3s-cluster-dashboard-template.json"
DASHBOARD_OUTPUT="${SCRIPT_DIR}/../dashboards/k3s-cluster-dashboard.json"
REGION="${AWS_REGION:-ap-southeast-1}"
DASHBOARD_NAME="k3s-cluster"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() { echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $*"; }
warn() { echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} WARNING: $*"; }
error() { echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} ERROR: $*"; exit 1; }

# Check if AWS profile is set
if [[ -z "${AWS_PROFILE:-}" && -z "${AWS_ACCESS_KEY_ID:-}" ]]; then
    error "AWS_PROFILE or AWS_ACCESS_KEY_ID must be set"
fi

log "Starting dashboard update for region: $REGION"

# Step 1: Query Lambda functions (for log queries only)
log "Querying Lambda functions..."
DECISION_LAMBDA=$(aws lambda list-functions --region "$REGION" --query "Functions[?contains(FunctionName, 'k3s-autoscaler-function')].FunctionName" --output text 2>/dev/null | head -1)
SCALE_UP_LAMBDA=$(aws lambda list-functions --region "$REGION" --query "Functions[?contains(FunctionName, 'scale-up-lambda')].FunctionName" --output text 2>/dev/null | head -1)
SCALE_DOWN_LAMBDA=$(aws lambda list-functions --region "$REGION" --query "Functions[?contains(FunctionName, 'scale-down-lambda')].FunctionName" --output text 2>/dev/null | head -1)

[[ -z "$DECISION_LAMBDA" ]] && error "Decision Lambda not found"
[[ -z "$SCALE_UP_LAMBDA" ]] && error "Scale-Up Lambda not found"
[[ -z "$SCALE_DOWN_LAMBDA" ]] && error "Scale-Down Lambda not found"

log "Found Lambda functions:"
log "  - Decision: $DECISION_LAMBDA"
log "  - Scale-Up: $SCALE_UP_LAMBDA"
log "  - Scale-Down: $SCALE_DOWN_LAMBDA"

# Step 2: Update dashboard JSON with log group names
log "Updating dashboard JSON with Lambda names..."

# Use template as starting point
if [[ ! -f "$DASHBOARD_TEMPLATE" ]]; then
    error "Template file not found: $DASHBOARD_TEMPLATE"
fi

# Copy template to output
cp "$DASHBOARD_TEMPLATE" "$DASHBOARD_OUTPUT"

# Update Lambda function names in log queries
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS sed
    sed -i '' "s|/aws/lambda/k3s-autoscaler-function-[a-zA-Z0-9]*|/aws/lambda/$DECISION_LAMBDA|g" "$DASHBOARD_OUTPUT"
    sed -i '' "s|/aws/lambda/scale-up-lambda-[a-zA-Z0-9]*|/aws/lambda/$SCALE_UP_LAMBDA|g" "$DASHBOARD_OUTPUT"
    sed -i '' "s|/aws/lambda/scale-down-lambda-[a-zA-Z0-9]*|/aws/lambda/$SCALE_DOWN_LAMBDA|g" "$DASHBOARD_OUTPUT"
else
    # Linux sed
    sed -i "s|/aws/lambda/k3s-autoscaler-function-[a-zA-Z0-9]*|/aws/lambda/$DECISION_LAMBDA|g" "$DASHBOARD_OUTPUT"
    sed -i "s|/aws/lambda/scale-up-lambda-[a-zA-Z0-9]*|/aws/lambda/$SCALE_UP_LAMBDA|g" "$DASHBOARD_OUTPUT"
    sed -i "s|/aws/lambda/scale-down-lambda-[a-zA-Z0-9]*|/aws/lambda/$SCALE_DOWN_LAMBDA|g" "$DASHBOARD_OUTPUT"
fi

log "Dashboard JSON updated successfully"

# Step 3: Deploy dashboard
log "Deploying dashboard to CloudWatch..."
RESULT=$(aws cloudwatch put-dashboard \
    --dashboard-name "$DASHBOARD_NAME" \
    --region "$REGION" \
    --dashboard-body "file://$DASHBOARD_OUTPUT" 2>&1)

# Check for validation errors
if echo "$RESULT" | jq -e '.DashboardValidationMessages | length > 0' > /dev/null 2>&1; then
    WARNINGS=$(echo "$RESULT" | jq -r '.DashboardValidationMessages[]')
    warn "Dashboard deployed with validation warnings:"
    echo "$WARNINGS"
else
    log "✅ Dashboard deployed successfully with no validation errors!"
fi

# Output dashboard URL
echo ""
log "Dashboard URL: https://console.aws.amazon.com/cloudwatch/home?region=$REGION#dashboards:name=$DASHBOARD_NAME"

log "Done!"
