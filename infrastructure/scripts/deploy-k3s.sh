#!/bin/bash
# Ansible integration script for K3s cluster deployment
# This script runs after Pulumi deployment to configure the K3s cluster

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"
ANSIBLE_DIR="$INFRA_DIR/ansible"
KUBECONFIG_DIR="/mnt/e/custom_autoscaler/production/cluster"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

# Check prerequisites
for cmd in ansible jq kubectl helm; do
    if ! command -v $cmd &> /dev/null; then
        log_error "$cmd is not installed. Please install it first:"
        case $cmd in
            ansible) echo "  pip install ansible-core" ;;
            jq) echo "  sudo apt-get install jq  # Ubuntu/Debian" ;;
            kubectl) echo "  curl -LO https://dl.k8s.io/release/\$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl && sudo install kubectl /usr/local/bin/" ;;
            helm) echo "  curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash" ;;
        esac
        exit 1
    fi
done

# Change to pulumi directory
cd "$INFRA_DIR/pulumi"

# Get Pulumi outputs
log_info "Getting infrastructure details from Pulumi..."
OUTPUTS=$(pulumi stack output -j)

if [ -z "$OUTPUTS" ]; then
    log_error "Failed to get Pulumi outputs. Make sure 'pulumi up' has been run."
    exit 1
fi

# Extract IPs and S3 bucket using jq
MASTER_IP=$(echo "$OUTPUTS" | jq -r '.master_public_ip.value // empty')
WORKER_1_IP=$(echo "$OUTPUTS" | jq -r '.worker_1_public_ip.value // empty')
WORKER_2_IP=$(echo "$OUTPUTS" | jq -r '.worker_2_public_ip.value // empty')
S3_BUCKET=$(echo "$OUTPUTS" | jq -r '.s3_config_bucket.value // empty')

# Validate we got the IPs
if [ -z "$MASTER_IP" ] || [ -z "$WORKER_1_IP" ] || [ -z "$WORKER_2_IP" ] || [ -z "$S3_BUCKET" ]; then
    log_error "Failed to extract infrastructure details from Pulumi outputs."
    log_error "Master IP: $MASTER_IP"
    log_error "Worker 1 IP: $WORKER_1_IP"
    log_error "Worker 2 IP: $WORKER_2_IP"
    log_error "S3 Bucket: $S3_BUCKET"
    exit 1
fi

log_info "Extracted infrastructure details:"
echo "  Master:    $MASTER_IP"
echo "  Worker 1:  $WORKER_1_IP"
echo "  Worker 2:  $WORKER_2_IP"
echo "  S3 Bucket: $S3_BUCKET"

# Check if key pair file exists
KEY_FILE="${ANSIBLE_KEY_FILE:-~/.ssh/my-key-pair.pem}"
if [ ! -f "$KEY_FILE" ]; then
    log_warn "SSH key file not found at: $KEY_FILE"
    log_warn "Set ANSIBLE_KEY_FILE environment variable to specify key location."
    echo ""
    read -p "Press Enter to continue or Ctrl+C to abort..."
fi

# Create inventory directory
mkdir -p "$ANSIBLE_DIR/inventory"

# Generate Ansible inventory from template
log_info "Generating Ansible inventory file..."

sed -e "s|__MASTER_IP__|$MASTER_IP|g" \
    -e "s|__WORKER_1_IP__|$WORKER_1_IP|g" \
    -e "s|__WORKER_2_IP__|$WORKER_2_IP|g" \
    -e "s|__KEY_FILE__|$KEY_FILE|g" \
    -e "s|__S3_BUCKET__|$S3_BUCKET|g" \
    "$ANSIBLE_DIR/inventory/hosts.ini.template" > "$ANSIBLE_DIR/inventory/hosts.ini"

log_info "Inventory file created at: $ANSIBLE_DIR/inventory/hosts.ini"

# Wait for instances to be ready
log_info "Waiting for instances to be ready (this may take 2-3 minutes)..."

wait_for_ssh() {
    local host=$1
    local max_attempts=60
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY_FILE" ubuntu@$host "echo ready" &> /dev/null; then
            log_info "✓ $host is ready"
            return 0
        fi
        echo -n "."
        sleep 3
        attempt=$((attempt + 1))
    done

    log_error "$host did not become ready in time"
    return 1
}

wait_for_ssh "$MASTER_IP"
wait_for_ssh "$WORKER_1_IP"
wait_for_ssh "$WORKER_2_IP"

echo ""

# Run Ansible playbook
log_info "Running Ansible playbook to deploy K3s cluster with Prometheus..."
echo ""

cd "$ANSIBLE_DIR"
ansible-playbook -i inventory/hosts.ini site.yml

# Set up local kubeconfig
log_info "Setting up local kubeconfig..."
mkdir -p "$KUBECONFIG_DIR"
scp -i "$KEY_FILE" ubuntu@$MASTER_IP:/etc/rancher/k3s/k3s.yaml "$KUBECONFIG_DIR/kubeconfig"

# Update kubeconfig with the master IP
sed -i "s|127.0.0.1|$MASTER_IP|g" "$KUBECONFIG_DIR/kubeconfig"

log_info "K3s cluster deployment completed!"
echo ""
log_info "Cluster Summary:"
echo "  Kubeconfig: $KUBECONFIG_DIR/kubeconfig"
echo "  Master IP:  $MASTER_IP"
echo ""
log_info "To access the cluster:"
echo "  export KUBECONFIG=$KUBECONFIG_DIR/kubeconfig"
echo "  kubectl get nodes"
echo ""
log_info "To verify Prometheus is running:"
echo "  kubectl get pods -n prometheus"
echo "  kubectl get svc prometheus-server -n prometheus"
