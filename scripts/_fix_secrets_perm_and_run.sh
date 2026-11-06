#!/bin/bash
# 1. Allow k3s-worker role to manage its own secrets in AWS Secrets Manager
# 2. Create the join-token secret manually
# 3. Run the rest of site.yml against the workers

set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1

# Make sure the role policy allows secretsmanager:Describe/Create/Put on
# secrets starting with `k3s-`.
POLICY_NAME=k3s-worker-secretsmanager
cat > /tmp/k3s-secrets-policy.json <<JSON
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:DescribeSecret",
                "secretsmanager:CreateSecret",
                "secretsmanager:PutSecretValue",
                "secretsmanager:UpdateSecret",
                "secretsmanager:GetSecretValue",
                "secretsmanager:RestoreSecret"
            ],
            "Resource": "arn:aws:secretsmanager:ap-southeast-1:087666071439:secret:k3s-*"
        }
    ]
}
JSON

# Create or update the inline policy
echo "[*] Upserting inline policy $POLICY_NAME on role k3s-worker"
if aws iam get-role-policy --role-name k3s-worker --policy-name "$POLICY_NAME" >/dev/null 2>&1; then
    aws iam put-role-policy --role-name k3s-worker --policy-name "$POLICY_NAME" \
        --policy-document file:///tmp/k3s-secrets-policy.json
else
    aws iam put-role-policy --role-name k3s-worker --policy-name "$POLICY_NAME" \
        --policy-document file:///tmp/k3s-secrets-policy.json
fi

# Fetch the join token from the master + store it
JOIN_TOKEN=$(ANSIBLE_CONFIG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg \
    ansible k3s-master -a 'sudo cat /var/lib/rancher/k3s/server/node-token' --become 2>&1 \
    | grep -v '^k3s-master' | grep -v '^$' | tr -d ' ')
echo "[*] Join token length: ${#JOIN_TOKEN}"

echo "[*] Re-running site.yml master play"
ANSIBLE_CONFIG=/mnt/e/custom_autoscaler/production/scripts/elastikube-test-ansible.cfg \
ansible-playbook /mnt/e/custom_autoscaler/production/infrastructure/ansible/site.yml \
  --limit master \
  -e AWS_PROFILE=poridhi-aws \
  -e aws_region=ap-southeast-1 \
  -e cluster_name=production-k3s \
  2>&1 | tail -25
