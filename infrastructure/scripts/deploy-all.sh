#!/bin/bash
# Complete deployment script for K3s autoscaler infrastructure
# This script orchestrates: Pulumi infrastructure provisioning + Ansible cluster setup

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "${BLUE}==> ${NC}$1"
}

# Print banner
echo -e "${BLUE}"
echo "╔════════════════════════════════════════════════════════════╗"
echo "║        K3s Autoscaler - Complete Deployment                 ║"
echo "║   Infrastructure (Pulumi) + Cluster Setup (Ansible)        ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo ""

# Check prerequisites
log_step "Checking prerequisites..."

if ! command -v pulumi &> /dev/null; then
    log_error "Pulumi is not installed. Please install it first:"
    echo "  curl -fsSL https://get.pulumi.com | sh"
    exit 1
fi

if ! command -v ansible &> /dev/null; then
    log_error "Ansible is not installed. Please install it first:"
    echo "  pip install ansible-core"
    exit 1
fi

if ! command -v jq &> /dev/null; then
    log_error "jq is not installed. Please install it first:"
    echo "  sudo apt-get install jq  # Ubuntu/Debian"
    echo "  brew install jq           # macOS"
    exit 1
fi

log_info "All prerequisites installed ✓"
echo ""

# Step 1: Build Lambda package
log_step "Step 1/3: Building Lambda deployment package..."
cd "$INFRA_DIR/../lambda"
if [ ! -f "build/lambda.zip" ]; then
    ./build.sh
else
    log_info "Lambda package already exists (run ./build.sh to rebuild)"
fi
echo ""

# Step 2: Deploy infrastructure with Pulumi
log_step "Step 2/3: Deploying infrastructure with Pulumi..."
cd "$INFRA_DIR/pulumi"

log_info "Running 'pulumi up'..."
echo ""
pulumi up

# Check if pulumi up succeeded
if [ $? -ne 0 ]; then
    log_error "Pulumi deployment failed. Please fix the errors and try again."
    exit 1
fi
echo ""

# Step 3: Deploy K3s cluster with Ansible
log_step "Step 3/3: Deploying K3s cluster with Ansible..."
echo ""

read -p "$(log_info "Do you want to proceed with K3s cluster deployment via Ansible? (y/n) " )" -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    log_warn "Skipping Ansible deployment."
    log_info "You can run it later with: $SCRIPT_DIR/deploy-k3s.sh"
    echo ""
    log_info "Infrastructure deployed! Summary:"
    echo "  pulumi stack output"
    exit 0
fi

# Run the Ansible deployment script
exec "$SCRIPT_DIR/deploy-k3s.sh"
