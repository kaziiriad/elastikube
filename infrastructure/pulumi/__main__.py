"""
K3s Autoscaler Infrastructure

This Pulumi program provisions AWS resources for the K3s autoscaler:
- DynamoDB tables for state management and WAL
- SSM Parameter Store for cluster configuration (master IP)
- Secrets Manager for sensitive data (K3s join token)
- Lambda function for autoscaling decisions
- EventBridge rule for triggering
- CloudWatch monitoring and alarms
- VPC and networking (optional, if cluster doesn't exist)
"""

from datetime import datetime, timezone
import json
import os
import pathlib
import pulumi
import pulumi_command as command
from pulumi_command import local
import pulumi_aws as aws
from pulumi_aws import ec2, lambda_, dynamodb, iam, ssm, secretsmanager, s3, sns

# If CustomTimeouts is provider-specific (e.g., AWS, Kubernetes):

# =============================================================================
# Configuration
# =============================================================================
config = pulumi.Config()

# Get AWS account ID for resource naming
caller_identity = aws.get_caller_identity()
account_id = caller_identity.account_id

# AWS Region
region = config.get("aws:region", "ap-southeast-1")

# K3s Configuration
cluster_name = config.get("k3s:clusterName", "production-k3s")
min_nodes = config.get_int("k3s:minNodes", 2)
max_nodes = config.get_int("k3s:maxNodes", 10)

# EC2 Configuration
master_instance_type = config.get("ec2:masterInstanceType", "t3.small")  # Free Tier eligible
worker_instance_type = config.get("ec2:workerInstanceType", "t3.small")
# Ubuntu 22.04 AMI IDs (update if using different region)
ami_id = config.get("ec2:amiId", "ami-0c687e8f5c4e54af5")  # ap-southeast-1 (Ubuntu 22.04)

# Autoscaler Configuration
check_interval = config.get_int("autoscaler:checkInterval", 120)
scale_up_threshold = config.get_int("autoscaler:scaleUpThreshold", 70)
scale_down_threshold = config.get_int("autoscaler:scaleDownThreshold", 30)
scale_up_cooldown = config.get_int("autoscaler:scaleUpCooldown", 300)
scale_down_cooldown = config.get_int("autoscaler:scaleDownCooldown", 900)

# Alarm Notification Configuration
alarm_email = config.get("alarm:email", None)  # Optional: Set to your email for alarm notifications

# =============================================================================
# Tags
# =============================================================================
common_tags = {
    "Project": "k3s-autoscaler",
    "Cluster": cluster_name,
    "ManagedBy": "pulumi",
}

# =============================================================================
# VPC and Networking (Optional - for new cluster deployment)
# =============================================================================
# TODO: Configure VPC networking based on your existing setup
# Create a VPC
# NOTE: VPC must be created FIRST in code (so it deletes LAST during pulumi destroy)
# This ensures all dependent resources (Lambdas, EC2, SGs, subnets) delete before VPC
vpc = ec2.Vpc(
    'my-vpc',
    cidr_block='10.0.0.0/16',
    enable_dns_hostnames=True,
    enable_dns_support=True,
    tags={
        'Name': 'my-vpc',
    },
    opts=pulumi.ResourceOptions(custom_timeouts=pulumi.CustomTimeouts(create="5m", delete="15m")),
)

# =============================================================================
# Multi-AZ Subnet Configuration
# =============================================================================
# We create subnets in 3 AZs for high availability:
# - ap-southeast-1a: Primary AZ (master + permanent workers)
# - ap-southeast-1b: Secondary AZ (scaled workers)
# - ap-southeast-1c: Tertiary AZ (scaled workers)
#
# Scaled workers are distributed using round-robin across all 3 AZs.
# Single NAT Gateway in AZ-a for cost optimization (private subnets route to it).
# =============================================================================

# Create subnets
public_subnet = ec2.Subnet('public-subnet',
    vpc_id=vpc.id,
    cidr_block='10.0.1.0/24',
    map_public_ip_on_launch=True,
    availability_zone='ap-southeast-1a',
    tags={
        'Name': 'public-subnet',
        'AvailabilityZone': 'ap-southeast-1a',
    },
    opts=pulumi.ResourceOptions(custom_timeouts=pulumi.CustomTimeouts(create="2m", delete="10m")),
)

# Private subnet in AZ-a (10.0.2.0/24)
# Contains: Master node + permanent workers (k3s-worker-1, k3s-worker-2)
private_subnet_a = ec2.Subnet('private-subnet-a',
    vpc_id=vpc.id,
    cidr_block='10.0.2.0/24',
    map_public_ip_on_launch=False,
    availability_zone='ap-southeast-1a',
    tags={
        'Name': 'private-subnet-a',
        'AvailabilityZone': 'ap-southeast-1a',
        'Type': 'primary',  # Primary AZ for master and permanent workers
    },
    opts=pulumi.ResourceOptions(custom_timeouts=pulumi.CustomTimeouts(create="2m", delete="10m")),
)

# Private subnet in AZ-b (10.0.3.0/24)
# Contains: Scaled workers (round-robin distribution)
private_subnet_b = ec2.Subnet('private-subnet-b',
    vpc_id=vpc.id,
    cidr_block='10.0.3.0/24',
    map_public_ip_on_launch=False,
    availability_zone='ap-southeast-1b',
    tags={
        'Name': 'private-subnet-b',
        'AvailabilityZone': 'ap-southeast-1b',
        'Type': 'scaled',  # For scaled workers only
    },
    opts=pulumi.ResourceOptions(custom_timeouts=pulumi.CustomTimeouts(create="2m", delete="10m")),
)

# Private subnet in AZ-c (10.0.4.0/24)
# Contains: Scaled workers (round-robin distribution)
private_subnet_c = ec2.Subnet('private-subnet-c',
    vpc_id=vpc.id,
    cidr_block='10.0.4.0/24',
    map_public_ip_on_launch=False,
    availability_zone='ap-southeast-1c',
    tags={
        'Name': 'private-subnet-c',
        'AvailabilityZone': 'ap-southeast-1c',
        'Type': 'scaled',  # For scaled workers only
    },
    opts=pulumi.ResourceOptions(custom_timeouts=pulumi.CustomTimeouts(create="2m", delete="10m")),
)

# For backward compatibility - keep the old reference
private_subnet = private_subnet_a

# Internet Gateway
igw = ec2.InternetGateway('internet-gateway', vpc_id=vpc.id)

# Route Table for Public Subnet
public_route_table = ec2.RouteTable('public-route-table', 
    vpc_id=vpc.id,
    routes=[{
        'cidr_block': '0.0.0.0/0',
        'gateway_id': igw.id,
    }],
    tags={
        'Name': 'public-route-table',
    }
)

# Associate the public route table with the public subnet
public_route_table_association = ec2.RouteTableAssociation(
    'public-route-table-association',
    subnet_id=public_subnet.id,
    route_table_id=public_route_table.id,
    # Ensure route table association deletes before subnet (helps with cleanup)
    opts=pulumi.ResourceOptions(delete_before_replace=True, custom_timeouts=pulumi.CustomTimeouts(create="2m", delete="5m")),
)

# Elastic IP for NAT Gateway
eip = ec2.Eip(
    'nat-eip',
    tags={'Name': 'k3s-deployment-eip'},
    opts=pulumi.ResourceOptions(custom_timeouts=pulumi.CustomTimeouts(create="2m", delete="5m")),
)

# NAT Gateway
nat_gateway = ec2.NatGateway(
    'nat-gateway',
    subnet_id=public_subnet.id,
    allocation_id=eip.id,
    tags={
        'Name': 'nat-gateway',
    },
    opts=pulumi.ResourceOptions(custom_timeouts=pulumi.CustomTimeouts(create="5m", delete="10m")),
)

# Route Table for Private Subnet (shared by all AZs for cost optimization)
# All private subnets route to the single NAT Gateway in AZ-a
private_route_table = ec2.RouteTable(
    'private-route-table',
    vpc_id=vpc.id,
    routes=[{
        'cidr_block': '0.0.0.0/0',
        'nat_gateway_id': nat_gateway.id,
    }],
    tags={
        'Name': 'private-route-table',
    }
)

# Associate the private route table with all 3 private subnets
# This allows all private subnets to use the single NAT Gateway in AZ-a
private_route_table_association_a = ec2.RouteTableAssociation(
    'private-route-table-association-a',
    subnet_id=private_subnet_a.id,
    route_table_id=private_route_table.id,
    # Ensure route table association deletes before subnet (helps with cleanup)
    opts=pulumi.ResourceOptions(delete_before_replace=True, custom_timeouts=pulumi.CustomTimeouts(create="2m", delete="5m")),
)

private_route_table_association_b = ec2.RouteTableAssociation(
    'private-route-table-association-b',
    subnet_id=private_subnet_b.id,
    route_table_id=private_route_table.id,
    opts=pulumi.ResourceOptions(delete_before_replace=True, custom_timeouts=pulumi.CustomTimeouts(create="2m", delete="5m")),
)

private_route_table_association_c = ec2.RouteTableAssociation(
    'private-route-table-association-c',
    subnet_id=private_subnet_c.id,
    route_table_id=private_route_table.id,
    opts=pulumi.ResourceOptions(delete_before_replace=True, custom_timeouts=pulumi.CustomTimeouts(create="2m", delete="5m")),
)

# For backward compatibility - keep the old reference
private_route_table_association = private_route_table_association_a

# Security Group for K3s Cluster (bastion + cluster nodes + monitoring)
# Merged security group to avoid slow deployment from cross-references
# NOTE: This must be defined AFTER EC2 instances in the code to ensure proper deletion order
# Pulumi deletes in reverse order of creation, so we want SG to be created BEFORE instances
bastion_security_group = aws.ec2.SecurityGroup("k3s-cluster-secgrp",
    description='K3s cluster security group - bastion SSH, cluster internal traffic, monitoring',
    vpc_id=vpc.id,
    ingress=[
        # SSH access from internet (restrict to your IP in production)
        {
            "protocol": "tcp",
            "from_port": 22,
            "to_port": 22,
            "cidr_blocks": ["0.0.0.0/0"],
        },
        # Kubernetes API Server (within VPC)
        {
            "protocol": "tcp",
            "from_port": 6443,
            "to_port": 6443,
            "cidr_blocks": ["10.0.0.0/16"],
        },
        # Prometheus (for autoscaler access - within VPC)
        {
            "protocol": "tcp",
            "from_port": 9090,
            "to_port": 9090,
            "cidr_blocks": ["10.0.0.0/16"],
        },
        # Prometheus NodePort (autoscaler queries this - within VPC)
        {
            "protocol": "tcp",
            "from_port": 30900,
            "to_port": 30900,
            "cidr_blocks": ["10.0.0.0/16"],
        },
        # K3s flannel VXLAN (for pod network)
        {
            "protocol": "udp",
            "from_port": 8472,
            "to_port": 8472,
            "cidr_blocks": ["10.0.0.0/16"],
        },
        # NodePort services range
        {
            "protocol": "tcp",
            "from_port": 30000,
            "to_port": 32767,
            "cidr_blocks": ["10.0.0.0/16"],
        },
        # Node Exporter (for Prometheus metrics - within cluster)
        {
            "protocol": "tcp",
            "from_port": 9100,
            "to_port": 9100,
            "self": True,  # Allow nodes to reach each other
        },
        # Kubelet / cAdvisor (for container metrics - within cluster)
        {
            "protocol": "tcp",
            "from_port": 10250,
            "to_port": 10250,
            "self": True,  # Allow nodes to reach each other
        },
    ],
    egress=[{
        "protocol": "-1",
        "from_port": 0,
        "to_port": 0,
        "cidr_blocks": ["0.0.0.0/0"],
    }],
    tags={
        'Name': 'k3s-cluster-secgrp',
    },
    opts=pulumi.ResourceOptions(custom_timeouts=pulumi.CustomTimeouts(create="2m", delete="10m")),
)

# =============================================================================
# DynamoDB Tables
# =============================================================================

# Cluster State Table
cluster_state_table = dynamodb.Table(
    "k3s-cluster-state",
    attributes=[
        dynamodb.TableAttributeArgs(name="cluster_id", type="S"),
        dynamodb.TableAttributeArgs(name="scaling_in_progress", type="S"),
        dynamodb.TableAttributeArgs(name="last_scale_time", type="S"),
    ],
    hash_key="cluster_id",
    billing_mode="PAY_PER_REQUEST",
    ttl=dynamodb.TableTtlArgs(
        attribute_name="ttl",
        enabled=True,
    ),
    point_in_time_recovery=dynamodb.TablePointInTimeRecoveryArgs(
        enabled=True,
    ),
    tags={**common_tags, "Name": "k3s-cluster-state"},
    global_secondary_indexes=[
        # Index for querying by scaling status
        dynamodb.TableGlobalSecondaryIndexArgs(
            name="ScalingStatusIndex",
            hash_key="scaling_in_progress",
            range_key="last_scale_time",
            projection_type="ALL",
        )
    ]
)

# Write-Ahead Log Table
wal_table = dynamodb.Table(
    "k3s-scaling-wal",
    attributes=[
        dynamodb.TableAttributeArgs(name="operation_id", type="S"),
        dynamodb.TableAttributeArgs(name="started_at", type="S"),
        dynamodb.TableAttributeArgs(name="state", type="S"),
    ],
    hash_key="operation_id",
    range_key="started_at",
    billing_mode="PAY_PER_REQUEST",
    ttl=dynamodb.TableTtlArgs(
        attribute_name="ttl",
        enabled=True,
    ),
    global_secondary_indexes=[
        dynamodb.TableGlobalSecondaryIndexArgs(
            name="IncompleteOperations",
            hash_key="state",
            range_key="started_at",
            projection_type="ALL",
        )
    ],
    tags={**common_tags, "Name": "k3s-scaling-wal"},
)

# =============================================================================
# SSM Parameter Store & Secrets Manager for Node Join
# =============================================================================

# SSM Parameter for Master IP (public configuration)
# Note: Uses static IP assigned to master instance (defined below at line 674)
master_ip_parameter = ssm.Parameter(
    "k3s-master-ip",
    name=f"/k3s/{cluster_name}/master-ip",
    type="String",
    value="10.0.2.10",  # Static IP for master node (matches master_static_ip)
    overwrite=True,  # Allow overwriting existing parameter
    description="K3s master node private IP for worker node join",
    tags={**common_tags, "Name": "k3s-master-ip-parameter"},
)

# Secrets Manager Secret for K3s Join Token (sensitive data)
# Note: recovery_window_in_days=0 for immediate cleanup during pulumi destroy
k3s_join_token_secret = secretsmanager.Secret(
    "k3s-join-token",
    name=f"k3s-{cluster_name}-join-token",
    description="K3s cluster join token for worker nodes",
    tags={**common_tags, "Name": "k3s-join-token-secret"},
    recovery_window_in_days=0,  # Immediate deletion, no recovery window (dev/test)
)

# Secret version (initial value, will be updated by Ansible)
k3s_join_token_version = secretsmanager.SecretVersion(
    "k3s-join-token-version",
    secret_id=k3s_join_token_secret.id,
    secret_string="PENDING",  # Will be populated by Ansible after cluster setup
)

# =============================================================================
# S3 Bucket for Worker Bootstrap Scripts
# =============================================================================
# Stores user-data scripts that new worker instances fetch during launch
# The actual bootstrap script is deployed via Ansible (worker-bootstrap.yml)
#
# Note: Bucket creation requires S3 permissions. If IAM user lacks permissions,
# the bucket can be created manually or via Ansible before running Pulumi.

# S3 Bucket for user-data scripts
worker_userdata_bucket = s3.Bucket(
    "k3s-worker-userdata",
    bucket=f"k3s-userdata-{cluster_name}",
    force_destroy=True,  # Delete all objects before destroying bucket
    tags={**common_tags, "Name": "k3s-worker-userdata", "Purpose": "worker-bootstrap-scripts"}
)

# =============================================================================
# IAM Roles
# =============================================================================

# Lambda Execution Role
lambda_assume_role = iam.get_policy_document(
    statements=[{
        "actions": ["sts:AssumeRole"],
        "effect": "Allow",
        "principals": [{
            "type": "Service",
            "identifiers": ["lambda.amazonaws.com"]
        }]
    }]
)

lambda_role = iam.Role(
    "k3s-autoscaler-lambda-role",
    assume_role_policy=lambda_assume_role.json,
    tags={**common_tags, "Name": "k3s-autoscaler-lambda-role"},
    opts=pulumi.ResourceOptions(delete_before_replace=True)
)

# Attach basic Lambda execution policy
lambda_role_policy_attachment = iam.RolePolicyAttachment(
    "k3s-autoscaler-lambda-basic-execution",
    role=lambda_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
)

# Attach VPC execution policy for network access
lambda_vpc_policy_attachment = iam.RolePolicyAttachment(
    "k3s-autoscaler-lambda-vpc-execution",
    role=lambda_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
)

# Custom policy for autoscaler permissions
autoscaler_policy = iam.RolePolicy(
    "k3s-autoscaler-lambda-policy",
    role=lambda_role.id,
    policy=pulumi.Output.all(
        cluster_state_arn=cluster_state_table.arn,
        wal_table_arn=wal_table.arn,
        userdata_bucket_arn=worker_userdata_bucket.arn,
    ).apply(lambda args: iam.get_policy_document(
        statements=[
            # EC2 Permissions
            {
                "actions": [
                    "ec2:RunInstances",
                    "ec2:TerminateInstances",
                    "ec2:DescribeInstances",
                    "ec2:DescribeInstanceStatus",
                    "ec2:CreateTags",
                    "ec2:DescribeTags",
                    # Spot Instance Permissions
                    "ec2:DescribeSpotInstanceRequests",
                    "ec2:DescribeSpotPriceHistory",
                    "ec2:RequestSpotInstances",
                    "ec2:CancelSpotInstanceRequests",
                ],
                "resources": ["*"],
                "effect": "Allow",
            },
            # DynamoDB Permissions
            {
                "actions": [
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:DeleteItem",
                ],
                "resources": [args["cluster_state_arn"], f"{args['cluster_state_arn']}/*"],
                "effect": "Allow",
            },
            {
                "actions": [
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:DeleteItem",
                ],
                "resources": [args["wal_table_arn"], f"{args['wal_table_arn']}/*"],
                "effect": "Allow",
            },
            # SSM Permissions
            {
                "actions": ["ssm:GetParameter"],
                "resources": [f"arn:aws:ssm:{region}:*:parameter/k3s/*"],
                "effect": "Allow",
            },
            # SSM Run Command Permissions (for kubectl drain via master)
            {
                "actions": ["ssm:SendCommand"],
                "resources": [
                    f"arn:aws:ssm:{region}:*:document/AWS-RunShellScript",
                    f"arn:aws:ec2:{region}:*:instance/*",
                ],
                "effect": "Allow",
            },
            # SSM GetCommandInvocation requires wildcard resource
            {
                "actions": ["ssm:GetCommandInvocation"],
                "resources": ["*"],
                "effect": "Allow",
            },
            # EventBridge Permissions (for Lambda chaining - publishing events)
            {
                "actions": ["events:PutEvents"],
                "resources": [f"arn:aws:events:{region}:*:event-bus/*"],
                "effect": "Allow",
            },
            # EventBridge Read Permissions (for observability and debugging)
            {
                "actions": [
                    "events:DescribeRule",
                    "events:ListRules",
                ],
                "resources": [f"arn:aws:events:{region}:*:rule/*"],
                "effect": "Allow",
            },
            {
                "actions": [
                    "events:DescribeEventBus",
                    "events:ListEventBuses",
                ],
                "resources": [f"arn:aws:events:{region}:*:event-bus/*"],
                "effect": "Allow",
            },
            # Secrets Manager Permissions
            {
                "actions": [
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:UpdateSecretVersionStage",
                ],
                "resources": [f"arn:aws:secretsmanager:{region}:*:secret:k3s-*-*"],
                "effect": "Allow",
            },
            # CloudWatch Logs Permissions
            {
                "actions": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "resources": ["arn:aws:logs:*:*:*"],
                "effect": "Allow",
            },
            # CloudWatch Metrics Permissions
            {
                "actions": [
                    "cloudwatch:PutMetricData",
                    "cloudwatch:GetMetricStatistics",
                    "cloudwatch:ListMetrics",
                ],
                "resources": ["*"],
                "effect": "Allow",
            },
            # S3 Permissions - Read user-data scripts for worker bootstrap
            {
                "actions": ["s3:GetObject"],
                "resources": [f"{args['userdata_bucket_arn']}/*"],
                "effect": "Allow",
            },
        ],
        version="2012-10-17",
    ).json)
)

# EC2 Instance Role for Worker Nodes
ec2_assume_role = iam.get_policy_document(
    statements=[{
        "actions": ["sts:AssumeRole"],
        "effect": "Allow",
        "principals": [{
            "type": "Service",
            "identifiers": ["ec2.amazonaws.com"]
        }]
    }]
)

worker_role = iam.Role(
    "k3s-worker-node-role",
    assume_role_policy=ec2_assume_role.json,
    tags={**common_tags, "Name": "k3s-worker-node-role"},
    opts=pulumi.ResourceOptions(delete_before_replace=True)
)

# Worker node policy
worker_policy = iam.RolePolicy(
    "k3s-worker-node-policy",
    role=worker_role.id,
    policy=pulumi.Output.all(
        cluster_state_arn=cluster_state_table.arn,
    ).apply(lambda args: iam.get_policy_document(
        statements=[
            {
                "actions": [
                    "dynamodb:UpdateItem",
                    "dynamodb:PutItem",
                ],
                "resources": [args["cluster_state_arn"]],
                "effect": "Allow",
            },
            {
                "actions": ["ssm:GetParameter"],
                "resources": [f"arn:aws:ssm:{region}:*:parameter/k3s/*"],
                "effect": "Allow",
            },
            {
                "actions": [
                    "ec2:DescribeInstances",
                    "ec2:DescribeTags",
                    "ec2:CreateTags",
                ],
                "resources": ["*"],
                "effect": "Allow",
            },
        ],
        version="2012-10-17",
    ).json)
)

# Attach SSM managed policy for Session Manager
worker_ssm_policy_attachment = iam.RolePolicyAttachment(
    "k3s-worker-ssm-policy",
    role=worker_role.name,
    policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
)

# Attach Secrets Manager read-only policy for fetching K3s join token during bootstrap
worker_secrets_manager_policy_attachment = iam.RolePolicyAttachment(
    "k3s-worker-secrets-manager-policy",
    role=worker_role.name,
    policy_arn="arn:aws:iam::aws:policy/AWSSecretsManagerClientReadOnlyAccess"
)

# Instance Profile for worker nodes
worker_instance_profile = iam.InstanceProfile(
    "k3s-worker-instance-profile",
    role=worker_role.name,
    tags={**common_tags, "Name": "k3s-worker-instance-profile"}
)

# IAM PassRole permission for Lambda to launch EC2 instances with instance profile
lambda_pass_role_policy = iam.RolePolicy(
    "k3s-autoscaler-lambda-pass-role",
    role=lambda_role.id,
    policy=iam.get_policy_document(
        statements=[{
            "actions": ["iam:PassRole"],
            "resources": [worker_role.arn],
            "effect": "Allow",
        }],
        version="2012-10-17",
    ).json
)

# =============================================================================
# Master Node IAM Role (for SSM access - kubectl drain)
# =============================================================================
master_role = iam.Role(
    "k3s-master-node-role",
    assume_role_policy=ec2_assume_role.json,
    tags={**common_tags, "Name": "k3s-master-node-role"},
    opts=pulumi.ResourceOptions(delete_before_replace=True)
)

# Attach SSM managed policy for Session Manager (kubectl drain via SSM)
master_ssm_policy_attachment = iam.RolePolicyAttachment(
    "k3s-master-ssm-policy",
    role=master_role.name,
    policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
)

# Attach Secrets Manager policy for storing K3s join token
master_secrets_manager_policy_attachment = iam.RolePolicyAttachment(
    "k3s-master-secrets-manager-policy",
    role=master_role.name,
    policy_arn="arn:aws:iam::aws:policy/SecretsManagerReadWrite"  # Or create inline policy for write-only
)

# Instance Profile for master node
master_instance_profile = iam.InstanceProfile(
    "k3s-master-instance-profile",
    role=master_role.name,
    tags={**common_tags, "Name": "k3s-master-instance-profile"}
)


master_cloudwatch_policy = iam.RolePolicy(
    "k3s-master-cloudwatch-policy",
    role=master_role.id,
    policy=iam.get_policy_document(
        statements=[
            {
                "actions": [
                    "cloudwatch:PutMetricData",
                    "ec2:DescribeVolumes",
                    "ec2:DescribeTags",
                    "logs:PutLogEvents",
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:DescribeLogStreams",
                ],
                "resources": ["*"],
                "effect": "Allow",
            },
        ],
        version="2012-10-17",
    ).json
)
# =============================================================================
# EC2 Instances
# =============================================================================
# Note: These are initial seed instances. The autoscaler Lambda will
# dynamically add/remove worker nodes based on cluster metrics.

# SSH Key Pair - use existing MyKeyPair in AWS
# Note: Key pair must already exist in AWS for this region
existing_key_name = config.get("ec2:keyPairName", "MyKeyPair")

# Static IP assignments for consistent infrastructure across deployments
# These IPs are reserved within the subnet CIDR blocks:
# - Public subnet: 10.0.1.0/24
# - Private subnet: 10.0.2.0/24
bastion_static_ip = "10.0.1.10"
master_static_ip = "10.0.2.10"
worker_1_static_ip = "10.0.2.11"
worker_2_static_ip = "10.0.2.12"

# Bastion Host (in public subnet for SSH access)
bastion_instance = ec2.Instance(
    'bastion-instance',
    instance_type="t3.micro",  # Smallest instance for bastion
    ami=ami_id,
    subnet_id=public_subnet.id,
    vpc_security_group_ids=[bastion_security_group.id],
    associate_public_ip_address=True,
    private_ip=bastion_static_ip,  # Static private IP for consistency
    key_name=existing_key_name,
    tags={**common_tags, 'Name': 'k3s-bastion', 'NodeRole': 'bastion'}
)

# Master Instance (in private subnet - accessed via bastion)
master_instance = ec2.Instance(
    'master-instance',
    instance_type=master_instance_type,
    ami=ami_id,
    subnet_id=private_subnet.id,  # Private subnet for security
    vpc_security_group_ids=[bastion_security_group.id],
    associate_public_ip_address=False,  # No public IP
    private_ip=master_static_ip,  # Static private IP for SSH config consistency
    iam_instance_profile=master_instance_profile.name,  # SSM access for kubectl drain
    key_name=existing_key_name,
    tags={**common_tags, 'Name': 'k3s-master', 'NodeRole': 'master'}
)

# Worker Instance 1 (permanent, in private subnet)
worker_instance_1 = ec2.Instance('worker-instance-1',
    instance_type=worker_instance_type,
    ami=ami_id,
    subnet_id=private_subnet.id,  # Private subnet for security
    vpc_security_group_ids=[bastion_security_group.id],
    associate_public_ip_address=False,  # No public IP
    private_ip=worker_1_static_ip,  # Static private IP
    iam_instance_profile=worker_instance_profile.name,
    key_name=existing_key_name,
    tags={**common_tags, 'Name': 'k3s-worker-1', 'NodeRole': 'worker', 'Permanent': 'true'}
)

# Worker Instance 2 (permanent, in private subnet)
worker_instance_2 = ec2.Instance('worker-instance-2',
    instance_type=worker_instance_type,
    ami=ami_id,
    subnet_id=private_subnet.id,  # Private subnet for security
    vpc_security_group_ids=[bastion_security_group.id],
    associate_public_ip_address=False,  # No public IP
    private_ip=worker_2_static_ip,  # Static private IP
    iam_instance_profile=worker_instance_profile.name,
    key_name=existing_key_name,
    tags={**common_tags, 'Name': 'k3s-worker-2', 'NodeRole': 'worker', 'Permanent': 'true'}
)

# =============================================================================
# EventBridge Rule
# =============================================================================

# EventBridge Rule - triggers every 2 minutes
event_rule = aws.cloudwatch.EventRule(
    "k3s-autoscaler-schedule",
    schedule_expression="rate(2 minutes)",
    tags={**common_tags, "Name": "k3s-autoscaler-schedule"}
)

# =============================================================================
# Lambda Function
# =============================================================================

# =============================================================================
# Build Decision Lambda Package
# =============================================================================
base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
decision_lambda_build = local.Command(
    "decision-lambda-build",
    create=f"cd {base_dir}/decision-lambda && ./build.sh",
    # delete=f"rm -rf {base_dir}/decision-lambda/build",
    triggers=[pulumi.FileAsset(f"{base_dir}/decision-lambda/main.py").path],
)

# Lambda deployment package (built by the command above)
lambda_archive = pulumi.FileArchive(f"{base_dir}/decision-lambda/build/lambda.zip")

# Lambda Function
lambda_function = lambda_.Function(
    "k3s-autoscaler-function",
    runtime="python3.11",
    handler="main.lambda_handler",
    role=lambda_role.arn,
    timeout=300,  # 5 minutes (max Lambda timeout)
    memory_size=256,  # 256 MB
    vpc_config=lambda_.FunctionVpcConfigArgs(
        subnet_ids=[private_subnet.id],
        security_group_ids=[bastion_security_group.id],
    ),
    environment=lambda_.FunctionEnvironmentArgs(
        variables={
            "CLUSTER_NAME": cluster_name,
            "PROMETHEUS_URL": master_instance.private_ip.apply(lambda ip: f"http://{ip}:30900"),
            "STATE_TABLE_NAME": cluster_state_table.name,
            "WAL_TABLE_NAME": wal_table.name,
            "EVENT_BUS_NAME": "default",  # EventBridge default event bus
            "MIN_NODES": str(min_nodes),
            "MAX_NODES": str(max_nodes),
            "SCALE_UP_THRESHOLD": str(scale_up_threshold),
            "SCALE_DOWN_THRESHOLD": str(scale_down_threshold),
            "SCALE_UP_COOLDOWN": str(scale_up_cooldown),
            "SCALE_DOWN_COOLDOWN": str(scale_down_cooldown),
            "DRY_RUN": "false",
            # EC2 Configuration
            "SUBNET_ID": private_subnet.id,
            "SECURITY_GROUP_ID": bastion_security_group.id,
            "IAM_INSTANCE_PROFILE": worker_instance_profile.name,
            "AMI_ID": ami_id,
            "INSTANCE_TYPE": worker_instance_type,
            # S3 Configuration for worker bootstrap script
            "USER_DATA_S3_BUCKET": worker_userdata_bucket.bucket,
            "USER_DATA_S3_KEY": "user-data/worker-bootstrap.sh",
        }
    ),
    code=lambda_archive,
    tags={**common_tags, "Name": "k3s-autoscaler"},
    opts=pulumi.ResourceOptions(depends_on=[decision_lambda_build]),
)

# Lambda Permission for EventBridge to invoke
lambda_permission = aws.lambda_.Permission(
    "k3s-autoscaler-eventbridge-permission",
    action="lambda:InvokeFunction",
    function=lambda_function.name,
    principal="events.amazonaws.com",
    source_arn=event_rule.arn,
)

# EventBridge Target - invokes Lambda
event_target = aws.cloudwatch.EventTarget(
    "k3s-autoscaler-target",
    rule=event_rule.name,
    arn=lambda_function.arn,
)

# =============================================================================
# Bootstrap Test Lambda (for testing worker bootstrap script)
# =============================================================================
# NOTE: Reuses the existing autoscaler Lambda IAM role to avoid iam:CreateRole permission issues
# The existing role already has EC2, S3, and CloudWatch permissions needed for bootstrap testing

# Reuse existing autoscaler Lambda IAM role (already has required permissions)
# The autoscaler_policy includes EC2, S3, and IAM PassRole permissions needed for scaling

# Build Scale-Up Lambda Package
scale_up_lambda_build = local.Command(
    "scale-up-lambda-build",
    create=f"cd {base_dir}/scale-up-lambda && ./build.sh",
    # delete=f"rm -rf {base_dir}/scale-up-lambda/build",
    triggers=[pulumi.FileAsset(f"{base_dir}/scale-up-lambda/main.py").path],
)

# Scale-Up Lambda deployment package (built by the command above)
scale_up_archive = pulumi.FileArchive(
    f"{base_dir}/scale-up-lambda/build/lambda.zip"
)

# Scale-Up Lambda Function
# Note: CloudWatch will automatically create a log group with the Lambda's name
scale_up_lambda = lambda_.Function(
    "scale-up-lambda",
    runtime="python3.11",
    handler="main.lambda_handler",
    role=lambda_role.arn,  # Reuse existing autoscaler Lambda role
    timeout=300,  # 5 minutes
    memory_size=256,  # 256 MB
    vpc_config=lambda_.FunctionVpcConfigArgs(
        subnet_ids=[private_subnet.id],
        security_group_ids=[bastion_security_group.id],
    ),
    environment=lambda_.FunctionEnvironmentArgs(
        variables={
            # Cluster Configuration
            "CLUSTER_NAME": cluster_name,
            "STATE_TABLE_NAME": cluster_state_table.name,
            # Multi-AZ Configuration
            # We provide all 3 subnet IDs for round-robin distribution
            "SUBNET_IDS": pulumi.Output.all(
                subnet_a_id=private_subnet_a.id,
                subnet_b_id=private_subnet_b.id,
                subnet_c_id=private_subnet_c.id,
            ).apply(lambda ids: json.dumps([ids["subnet_a_id"], ids["subnet_b_id"], ids["subnet_c_id"]])),
            # Primary subnet for backward compatibility (master + permanent workers)
            "SUBNET_ID": private_subnet_a.id,
            # Security Configuration
            "SECURITY_GROUP_ID": bastion_security_group.id,
            "IAM_INSTANCE_PROFILE": worker_instance_profile.name,
            "AMI_ID": ami_id,
            "INSTANCE_TYPE": worker_instance_type,
            "KEY_NAME": existing_key_name,  # SSH key pair for debugging
            # Spot Instance Configuration
            "USE_SPOT_INSTANCES": config.get_bool("use_spot_instances", False),
            # Bootstrap Verification Configuration
            "BOOTSTRAP_TIMEOUT_SECONDS": str(config.get_int("bootstrap_timeout", 180)),
            # S3 Configuration for bootstrap script
            "USER_DATA_S3_BUCKET": worker_userdata_bucket.bucket,
            "USER_DATA_S3_KEY": "user-data/worker-bootstrap.sh",
        }
    ),
    code=scale_up_archive,
    tags={**common_tags, "Name": "scale-up-lambda", "Purpose": "worker-scaling"},
    opts=pulumi.ResourceOptions(depends_on=[scale_up_lambda_build]),
)

# =============================================================================
# Scale-Down Lambda (for drain and scale-down operations)
# =============================================================================

# Reuse existing autoscaler Lambda IAM role
# The autoscaler_policy includes EC2, SSM, and CloudWatch permissions needed for scale-down

# Build Scale-Down Lambda Package
scale_down_lambda_build = local.Command(
    "scale-down-lambda-build",
    create=f"cd {base_dir}/scale-down-lambda && ./build.sh",
    # delete=f"rm -rf {base_dir}/scale-down-lambda/build",
    triggers=[pulumi.FileAsset(f"{base_dir}/scale-down-lambda/main.py").path],
)

# Scale-Down Lambda deployment package (built by the command above)
scale_down_archive = pulumi.FileArchive(
    f"{base_dir}/scale-down-lambda/build/lambda.zip"
)

# Scale-Down Lambda Function
# Note: CloudWatch will automatically create a log group with the Lambda's name
scale_down_lambda = lambda_.Function(
    "scale-down-lambda",
    runtime="python3.11",
    handler="main.lambda_handler",
    role=lambda_role.arn,  # Reuse existing autoscaler Lambda role
    timeout=300,  # 5 minutes
    memory_size=256,  # 256 MB
    vpc_config=lambda_.FunctionVpcConfigArgs(
        subnet_ids=[private_subnet.id],
        security_group_ids=[bastion_security_group.id],
    ),
    environment=lambda_.FunctionEnvironmentArgs(
        variables={
            # Cluster Configuration
            "CLUSTER_NAME": cluster_name,
            "STATE_TABLE_NAME": cluster_state_table.name,
        }
    ),
    code=scale_down_archive,
    tags={**common_tags, "Name": "scale-down-lambda", "Purpose": "worker-scaling"},
    opts=pulumi.ResourceOptions(depends_on=[scale_down_lambda_build]),
)

# =============================================================================
# Cleanup Lambda for Failed Instances
# =============================================================================

# Build Cleanup Lambda Package
cleanup_lambda_build = local.Command(
    "cleanup-lambda-build",
    create=f"cd {base_dir}/cleanup-lambda && ./build.sh",
    triggers=[pulumi.FileAsset(f"{base_dir}/cleanup-lambda/main.py").path],
)

# Cleanup Lambda deployment package
cleanup_archive = pulumi.FileArchive(
    f"{base_dir}/cleanup-lambda/build/lambda.zip"
)

# Cleanup Lambda Function
cleanup_lambda = lambda_.Function(
    "cleanup-lambda",
    runtime="python3.11",
    handler="main.lambda_handler",
    role=lambda_role.arn,  # Reuse existing autoscaler Lambda role (has EC2 permissions)
    timeout=60,  # 1 minute
    memory_size=128,  # 128 MB (lightweight)
    vpc_config=lambda_.FunctionVpcConfigArgs(
        subnet_ids=[private_subnet.id],
        security_group_ids=[bastion_security_group.id],
    ),
    environment=lambda_.FunctionEnvironmentArgs(
        variables={
            "CLUSTER_NAME": cluster_name,
            "MAX_INSTANCE_AGE_MINUTES": "15",  # Terminate instances that failed to join after 15 min
        }
    ),
    code=cleanup_archive,
    tags={**common_tags, "Name": "cleanup-lambda", "Purpose": "failed-instance-cleanup"},
    opts=pulumi.ResourceOptions(depends_on=[cleanup_lambda_build]),
)

# EventBridge Rule - triggers cleanup every 15 minutes
cleanup_event_rule = aws.cloudwatch.EventRule(
    "k3s-cleanup-schedule",
    schedule_expression="rate(15 minutes)",
    tags={**common_tags, "Name": "k3s-cleanup-schedule"}
)

# Lambda Permission for EventBridge to invoke cleanup
cleanup_lambda_permission = aws.lambda_.Permission(
    "cleanup-lambda-eventbridge-permission",
    action="lambda:InvokeFunction",
    function=cleanup_lambda.name,
    principal="events.amazonaws.com",
    source_arn=cleanup_event_rule.arn,
)

# EventBridge Target - invokes cleanup Lambda
cleanup_event_target = aws.cloudwatch.EventTarget(
    "k3s-cleanup-target",
    rule=cleanup_event_rule.name,
    arn=cleanup_lambda.arn,
)

# =============================================================================
# Spot Instance Interruption Handling
# =============================================================================

# EventBridge Rule - captures EC2 Spot Instance Interruption Warnings
# AWS sends this 2 minutes before terminating a spot instance
spot_interruption_rule = aws.cloudwatch.EventRule(
    "k3s-spot-interruption-handler",
    name_prefix="k3s-spot-interruption-",
    event_pattern=json.dumps({
        "source": ["aws.ec2"],
        "detail-type": ["EC2 Spot Instance Interruption Warning"],
    }),
    tags={**common_tags, "Name": "k3s-spot-interruption-handler"}
)

# Lambda Permission - Allow EventBridge to invoke cleanup Lambda for spot interruptions
spot_interruption_permission = aws.lambda_.Permission(
    "cleanup-lambda-spot-interruption-permission",
    action="lambda:InvokeFunction",
    function=cleanup_lambda.name,
    principal="events.amazonaws.com",
    source_arn=spot_interruption_rule.arn,
)

# EventBridge Target - invokes cleanup Lambda for spot interruptions
spot_interruption_target = aws.cloudwatch.EventTarget(
    "k3s-spot-interruption-target",
    rule=spot_interruption_rule.name,
    arn=cleanup_lambda.arn,
)

# =============================================================================
# Dead-Letter Queues for Failed Events
# =============================================================================

# SQS Queue for failed scale-up events
scale_up_dlq = aws.sqs.Queue(
    "k3s-scale-up-dlq",
    message_retention_seconds=1209600,  # 14 days
    tags={**common_tags, "Name": "k3s-scale-up-dlq", "Purpose": "failed-events"},
)

# SQS Queue for failed scale-down events
scale_down_dlq = aws.sqs.Queue(
    "k3s-scale-down-dlq",
    message_retention_seconds=1209600,  # 14 days
    tags={**common_tags, "Name": "k3s-scale-down-dlq", "Purpose": "failed-events"},
)

# =============================================================================
# EventBridge Rules for Lambda Chaining
# =============================================================================

# EventBridge Rule - ScaleUp events -> scale-up-lambda
scale_up_rule = aws.cloudwatch.EventRule(
    "k3s-scale-up-rule",
    name_prefix="k3s-scale-up-",
    event_pattern=json.dumps({
        "source": ["k3s.autoscaler"],
        "detail-type": ["ScaleUp"],
    }),
    tags={**common_tags, "Name": "k3s-scale-up-rule"}
)

# EventBridge Target - scale-up-lambda for scale-up
scale_up_target = aws.cloudwatch.EventTarget(
    "k3s-scale-up-target",
    rule=scale_up_rule.name,
    arn=scale_up_lambda.arn,
    dead_letter_config=aws.cloudwatch.EventTargetDeadLetterConfigArgs(
        arn=scale_up_dlq.arn,
    ),
)

# Lambda Permission - Allow EventBridge to invoke scale-up-lambda
scale_up_lambda_permission = aws.lambda_.Permission(
    "scale-up-lambda-permission",
    action="lambda:InvokeFunction",
    function=scale_up_lambda.name,
    principal="events.amazonaws.com",
    source_arn=scale_up_rule.arn,
)

# EventBridge Rule - ScaleDown events -> scale-down-lambda
scale_down_rule = aws.cloudwatch.EventRule(
    "k3s-scale-down-rule",
    name_prefix="k3s-scale-down-",
    event_pattern=json.dumps({
        "source": ["k3s.autoscaler"],
        "detail-type": ["ScaleDown"],
    }),
    tags={**common_tags, "Name": "k3s-scale-down-rule"}
)

# EventBridge Target - scale-down-lambda for scale-down
scale_down_target = aws.cloudwatch.EventTarget(
    "k3s-scale-down-target",
    rule=scale_down_rule.name,
    arn=scale_down_lambda.arn,
    dead_letter_config=aws.cloudwatch.EventTargetDeadLetterConfigArgs(
        arn=scale_down_dlq.arn,
    ),
)

# Lambda Permission - Allow EventBridge to invoke scale-down-lambda
scale_down_lambda_permission = aws.lambda_.Permission(
    "scale-down-lambda-permission",
    action="lambda:InvokeFunction",
    function=scale_down_lambda.name,
    principal="events.amazonaws.com",
    source_arn=scale_down_rule.arn,
)

# =============================================================================
# SNS Topic for Alarm Notifications
# =============================================================================

# Create SNS topic for critical and warning alerts
alarm_topic = sns.Topic(
    "k3s-autoscaler-alarms",
    tags={**common_tags, "Name": "k3s-autoscaler-alarms", "Purpose": "alarm-notifications"}
)

# Email subscription (optional - set alarm:email config to enable)
# If alarm_email is set, automatically create email subscription
# Otherwise, you can manually subscribe after deployment:
# aws sns subscribe --topic-arn <alarm-topic-arn> --protocol email --notification-endpoint your-email@example.com
if alarm_email is not None:
    alarm_subscription_email = sns.TopicSubscription(
        "k3s-alarms-email-subscription",
        topic=alarm_topic.arn,
        protocol="email",
        endpoint=alarm_email,
    )

# =============================================================================
# CloudWatch Alarms
# =============================================================================

# High CPU Alarm
high_cpu_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-high-cluster-cpu-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=2,
    metric_name="ClusterCPU",
    namespace="K3sAutoscaler",
    period=300,  # 5 minutes
    statistic="Average",
    threshold=85.0,
    alarm_description="Triggered when cluster CPU exceeds 85% for 10 minutes",
    tags={**common_tags, "Name": "k3s-high-cluster-cpu-alarm"}
)

# Scaling Failure Alarm
scaling_failure_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-scaling-failure-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="ScalingErrors",
    namespace="K3sAutoscaler",
    period=600,  # 10 minutes
    statistic="Sum",
    threshold=3.0,
    alarm_description="Triggered when scaling errors exceed 3 in 10 minutes",
    tags={**common_tags, "Name": "k3s-scaling-failure-alarm"}
)

# Node Provisioning Timeout Alarm
provisioning_timeout_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-provisioning-timeout-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="NodeProvisioningTime",
    namespace="K3sAutoscaler",
    period=60,
    statistic="Average",
    threshold=600000,  # 10 minutes in milliseconds
    alarm_description="Triggered when node provisioning takes longer than 10 minutes",
    tags={**common_tags, "Name": "k3s-provisioning-timeout-alarm"}
)

# DynamoDB Lock Timeout Alarm
lock_timeout_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-lock-timeout-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="LockAge",
    namespace="K3sAutoscaler",
    period=60,
    statistic="Average",
    threshold=300,  # 5 minutes
    alarm_description="Triggered when DynamoDB lock is held longer than 5 minutes",
    tags={**common_tags, "Name": "k3s-lock-timeout-alarm"}
)

# =============================================================================
# EventBridge and SQS DLQ Monitoring Alarms
# =============================================================================

# EventBridge FailedInvocations Alarm for Scale-Up
scale_up_eventbridge_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-scale-up-eventbridge-failed-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="FailedInvocations",
    namespace="AWS/Events",
    period=300,  # 5 minutes
    statistic="Sum",
    threshold=1.0,
    alarm_description="Triggered when EventBridge fails to invoke scale-up Lambda",
    dimensions={
        "RuleName": scale_up_rule.name,
    },
    tags={**common_tags, "Name": "k3s-scale-up-eventbridge-failed-alarm", "Component": "eventbridge"}
)

# EventBridge FailedInvocations Alarm for Scale-Down
scale_down_eventbridge_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-scale-down-eventbridge-failed-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="FailedInvocations",
    namespace="AWS/Events",
    period=300,  # 5 minutes
    statistic="Sum",
    threshold=1.0,
    alarm_description="Triggered when EventBridge fails to invoke scale-down Lambda",
    dimensions={
        "RuleName": scale_down_rule.name,
    },
    tags={**common_tags, "Name": "k3s-scale-down-eventbridge-failed-alarm", "Component": "eventbridge"}
)

# Scale-Up DLQ Alarm - triggers when messages accumulate
scale_up_dlq_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-scale-up-dlq-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="ApproximateNumberOfMessagesVisible",
    namespace="AWS/SQS",
    period=300,  # 5 minutes
    statistic="Average",
    threshold=1.0,
    alarm_description="Triggered when scale-up DLQ has messages (failed Lambda invocations)",
    dimensions={
        "QueueName": scale_up_dlq.name,
    },
    tags={**common_tags, "Name": "k3s-scale-up-dlq-alarm", "Component": "sqs-dlq"}
)

# Scale-Up DLQ Age Alarm - triggers when old messages exist
scale_up_dlq_age_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-scale-up-dlq-age-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="ApproximateAgeOfOldestMessage",
    namespace="AWS/SQS",
    period=300,  # 5 minutes
    statistic="Average",
    threshold=3600,  # 1 hour
    alarm_description="Triggered when scale-up DLQ has messages older than 1 hour",
    dimensions={
        "QueueName": scale_up_dlq.name,
    },
    tags={**common_tags, "Name": "k3s-scale-up-dlq-age-alarm", "Component": "sqs-dlq"}
)

# Scale-Down DLQ Alarm - triggers when messages accumulate
scale_down_dlq_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-scale-down-dlq-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="ApproximateNumberOfMessagesVisible",
    namespace="AWS/SQS",
    period=300,  # 5 minutes
    statistic="Average",
    threshold=1.0,
    alarm_description="Triggered when scale-down DLQ has messages (failed Lambda invocations)",
    dimensions={
        "QueueName": scale_down_dlq.name,
    },
    tags={**common_tags, "Name": "k3s-scale-down-dlq-alarm", "Component": "sqs-dlq"}
)

# Scale-Down DLQ Age Alarm - triggers when old messages exist
scale_down_dlq_age_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-scale-down-dlq-age-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="ApproximateAgeOfOldestMessage",
    namespace="AWS/SQS",
    period=300,  # 5 minutes
    statistic="Average",
    threshold=3600,  # 1 hour
    alarm_description="Triggered when scale-down DLQ has messages older than 1 hour",
    dimensions={
        "QueueName": scale_down_dlq.name,
    },
    tags={**common_tags, "Name": "k3s-scale-down-dlq-age-alarm", "Component": "sqs-dlq"}
)

# =============================================================================
# Critical Failure Alarms - Lambda Execution Health
# =============================================================================

# Decision Lambda Errors - detects code failures, exceptions
decision_lambda_errors_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-decision-lambda-errors-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="Errors",
    namespace="AWS/Lambda",
    period=300,  # 5 minutes
    statistic="Sum",
    threshold=1.0,
    alarm_description="CRITICAL: Decision Lambda is throwing exceptions. Autoscaling decisions may not be executing.",
    dimensions={
        "FunctionName": lambda_function.name,
    },
    alarm_actions=[alarm_topic.arn],
    tags={**common_tags, "Name": "k3s-decision-lambda-errors-alarm", "Severity": "CRITICAL"}
)

# Decision Lambda Duration - detects timeout risk (approaching 300s max)
decision_lambda_duration_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-decision-lambda-duration-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=2,
    metric_name="Duration",
    namespace="AWS/Lambda",
    period=300,  # 5 minutes
    statistic="Maximum",
    threshold=240,  # 80% of 300s timeout
    alarm_description="WARNING: Decision Lambda duration exceeding 240s (80% of timeout). Risk of timeout.",
    dimensions={
        "FunctionName": lambda_function.name,
    },
    alarm_actions=[alarm_topic.arn],
    tags={**common_tags, "Name": "k3s-decision-lambda-duration-alarm", "Severity": "WARNING"}
)

# Scale-Up Lambda Errors - detects worker launch failures
scale_up_lambda_errors_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-scale-up-lambda-errors-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="Errors",
    namespace="AWS/Lambda",
    period=300,  # 5 minutes
    statistic="Sum",
    threshold=1.0,
    alarm_description="CRITICAL: Scale-Up Lambda is failing. Workers cannot be launched to handle load.",
    dimensions={
        "FunctionName": scale_up_lambda.name,
    },
    alarm_actions=[alarm_topic.arn],
    tags={**common_tags, "Name": "k3s-scale-up-lambda-errors-alarm", "Severity": "CRITICAL"}
)

# Scale-Down Lambda Errors - detects drain/terminate failures
scale_down_lambda_errors_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-scale-down-lambda-errors-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="Errors",
    namespace="AWS/Lambda",
    period=300,  # 5 minutes
    statistic="Sum",
    threshold=1.0,
    alarm_description="CRITICAL: Scale-Down Lambda is failing. Workers may not be draining/terminating properly.",
    dimensions={
        "FunctionName": scale_down_lambda.name,
    },
    alarm_actions=[alarm_topic.arn],
    tags={**common_tags, "Name": "k3s-scale-down-lambda-errors-alarm", "Severity": "CRITICAL"}
)

# Cleanup Lambda Errors - detects stale node cleanup failures
cleanup_lambda_errors_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-cleanup-lambda-errors-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=2,
    metric_name="Errors",
    namespace="AWS/Lambda",
    period=300,  # 5 minutes
    statistic="Sum",
    threshold=3.0,
    alarm_description="WARNING: Cleanup Lambda experiencing errors. Stale nodes may accumulate.",
    dimensions={
        "FunctionName": cleanup_lambda.name,
    },
    alarm_actions=[alarm_topic.arn],
    tags={**common_tags, "Name": "k3s-cleanup-lambda-errors-alarm", "Severity": "WARNING"}
)

# =============================================================================
# Infrastructure Health Alarms
# =============================================================================

# Pending Pods Stuck - detects when pods can't be scheduled (autoscaler broken)
pending_pods_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-pending-pods-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=3,
    metric_name="PendingPods",
    namespace="K3sAutoscaler",
    period=60,  # 1 minute
    statistic="Average",
    threshold=5.0,  # 5 or more pods pending for 3+ minutes
    alarm_description="CRITICAL: 5+ pods pending for 3+ minutes. Autoscaler may be failing to scale up.",
    alarm_actions=[alarm_topic.arn],
    tags={**common_tags, "Name": "k3s-pending-pods-alarm", "Severity": "CRITICAL"}
)

# WAL Stale Operations - detects crashed operations
wal_stale_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-wal-stale-operations-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="IncompleteOperations",
    namespace="K3sAutoscaler",
    period=300,  # 5 minutes
    statistic="Maximum",
    threshold=600,  # Operations incomplete for 10+ minutes
    alarm_description="WARNING: WAL has operations incomplete for 10+ minutes. Crash recovery may be needed.",
    alarm_actions=[alarm_topic.arn],
    tags={**common_tags, "Name": "k3s-wal-stale-operations-alarm", "Severity": "WARNING"}
)

# Node Count Drops Below Minimum - detects worker loss
node_count_minimum_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-node-count-minimum-alarm",
    comparison_operator="LessThanThreshold",
    evaluation_periods=2,
    metric_name="TotalNodes",
    namespace="K3sAutoscaler",
    period=60,  # 1 minute
    statistic="Average",
    threshold=2.0,  # Below min_nodes
    alarm_description="CRITICAL: Node count dropped below minimum (2). Worker nodes may have crashed.",
    alarm_actions=[alarm_topic.arn],
    tags={**common_tags, "Name": "k3s-node-count-minimum-alarm", "Severity": "CRITICAL"}
)

# Master Node Memory - detects master resource exhaustion
master_memory_alarm = aws.cloudwatch.MetricAlarm(
    "k3s-master-memory-alarm",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=2,
    metric_name="MasterMemoryPercent",
    namespace="K3sAutoscaler",
    period=300,  # 5 minutes
    statistic="Average",
    threshold=90.0,  # 90% memory usage
    alarm_description="WARNING: Master node memory usage > 90%. Control plane at risk.",
    alarm_actions=[alarm_topic.arn],
    tags={**common_tags, "Name": "k3s-master-memory-alarm", "Severity": "WARNING"}
)

# =============================================================================
# CloudWatch Dashboard
# =============================================================================

# Read comprehensive cluster dashboard from JSON file
dashboard_path = pathlib.Path(__file__).parent.parent.parent / "monitoring" / "dashboards" / "k3s-cluster-dashboard.json"

try:
    with open(dashboard_path, "r") as f:
        dashboard_body = json.dumps(json.load(f))
except FileNotFoundError:
    # Fallback to minimal dashboard if file not found
    dashboard_body = json.dumps({
        "widgets": [{
            "type": "metric",
            "x": 0, "y": 0, "width": 12, "height": 6,
            "properties": {
                "metrics": [["AWS/Lambda", "Invocations", "FunctionName", lambda_function.name]],
                "period": 300,
                "stat": "Sum",
                "region": aws.config.region,
                "title": "Lambda Invocations"
            }
        }]
    })

cluster_dashboard = aws.cloudwatch.Dashboard(
    "k3s-cluster-dashboard",
    dashboard_name="K3s-Cluster-Comprehensive",
    dashboard_body=dashboard_body
)

# =============================================================================
# Outputs
# =============================================================================
pulumi.export("cluster_name", cluster_name)
pulumi.export("dynamodb_cluster_state_table", cluster_state_table.name)
pulumi.export("dynamodb_wal_table", wal_table.name)
pulumi.export("lambda_role_arn", lambda_role.arn)
pulumi.export("lambda_function_arn", lambda_function.arn)
pulumi.export("lambda_function_name", lambda_function.name)
pulumi.export("autoscaler_lambda_logs_command", lambda_function.name.apply(
    lambda name: f"aws logs tail /aws/lambda/{name} --follow"
))
pulumi.export("decision_lambda_log_group", lambda_function.name.apply(
    lambda name: f"/aws/lambda/{name}"
))
pulumi.export("worker_instance_profile", worker_instance_profile.name)
pulumi.export("master_instance_profile", master_instance_profile.name)
pulumi.export("event_rule_arn", event_rule.arn)
pulumi.export("cloudwatch_dashboard", cluster_dashboard.dashboard_name)

# Security Group ID (now merged - single security group for cluster)
pulumi.export("security_group_id", bastion_security_group.id)

# Bastion Host (for SSH access)
pulumi.export("bastion_public_ip", bastion_instance.public_ip)
pulumi.export("bastion_private_ip", bastion_instance.private_ip)

# K3s Cluster Nodes (private IPs only - accessed via bastion)
pulumi.export("master_private_ip", master_instance.private_ip)
pulumi.export("worker_1_private_ip", worker_instance_1.private_ip)
pulumi.export("worker_2_private_ip", worker_instance_2.private_ip)

# Configuration outputs
pulumi.export("config_min_nodes", min_nodes)
pulumi.export("config_max_nodes", max_nodes)
pulumi.export("config_scale_up_threshold", scale_up_threshold)
pulumi.export("config_scale_down_threshold", scale_down_threshold)
pulumi.export("config_scale_up_cooldown", scale_up_cooldown)
pulumi.export("config_scale_down_cooldown", scale_down_cooldown)

# SSM and Secrets Manager exports
pulumi.export("ssm_master_ip_parameter_name", master_ip_parameter.name)
pulumi.export("secrets_manager_join_token_arn", k3s_join_token_secret.arn)

# S3 bucket for worker bootstrap scripts
pulumi.export("worker_userdata_bucket_name", worker_userdata_bucket.bucket)
pulumi.export("worker_userdata_bucket_arn", worker_userdata_bucket.arn)

# Scale-Up Lambda
pulumi.export("scale_up_lambda_name", scale_up_lambda.name)
pulumi.export("scale_up_lambda_arn", scale_up_lambda.arn)
pulumi.export("scale_up_lambda_log_group", scale_up_lambda.name.apply(
    lambda name: f"/aws/lambda/{name}"
))
pulumi.export("scale_up_lambda_logs_command", scale_up_lambda.name.apply(
    lambda name: f"aws logs tail /aws/lambda/{name} --follow"
))

# Scale-Down Lambda
pulumi.export("scale_down_lambda_name", scale_down_lambda.name)
pulumi.export("scale_down_lambda_arn", scale_down_lambda.arn)
pulumi.export("scale_down_lambda_log_group", scale_down_lambda.name.apply(
    lambda name: f"/aws/lambda/{name}"
))
pulumi.export("scale_down_lambda_logs_command", scale_down_lambda.name.apply(
    lambda name: f"aws logs tail /aws/lambda/{name} --follow"
))

# Cleanup Lambda
pulumi.export("cleanup_lambda_name", cleanup_lambda.name)
pulumi.export("cleanup_lambda_arn", cleanup_lambda.arn)
pulumi.export("cleanup_lambda_log_group", cleanup_lambda.name.apply(
    lambda name: f"/aws/lambda/{name}"
))
pulumi.export("cleanup_lambda_logs_command", cleanup_lambda.name.apply(
    lambda name: f"aws logs tail /aws/lambda/{name} --follow"
))

# EventBridge Rules for Lambda Chaining
pulumi.export("scale_up_rule_arn", scale_up_rule.arn)
pulumi.export("scale_up_rule_name", scale_up_rule.name)
pulumi.export("scale_down_rule_arn", scale_down_rule.arn)
pulumi.export("scale_down_rule_name", scale_down_rule.name)

# Dead-Letter Queues
pulumi.export("scale_up_dlq_url", scale_up_dlq.url)
pulumi.export("scale_up_dlq_arn", scale_up_dlq.arn)
pulumi.export("scale_down_dlq_url", scale_down_dlq.url)
pulumi.export("scale_down_dlq_arn", scale_down_dlq.arn)

# Alarm SNS Topic
pulumi.export("alarm_sns_topic_arn", alarm_topic.arn)
pulumi.export("alarm_sns_topic_name", alarm_topic.name)
pulumi.export("alarm_sns_subscribe_command", pulumi.Output.format(
    "aws sns subscribe --topic-arn {arn} --protocol email --notification-endpoint YOUR_EMAIL@example.com",
    arn=alarm_topic.arn
))


# =============================================================================
# SSH Config Generator
# =============================================================================
# Generates SSH config entries for K3s cluster access
# This can be appended to ~/.ssh/config for easy cluster access

def generate_ssh_config(bastion_public, master_private, worker1_private, worker2_private):
    """Generate SSH config entries for K3s cluster access.

    Args:
        bastion_public: Bastion public IP (for SSH access)
        master_private: Master node private IP
        worker1_private: Worker 1 private IP
        worker2_private: Worker 2 private IP

    Returns:
        SSH config content as string
    """
    return f"""# K3s Cluster Deployment (Auto-generated by Pulumi: {datetime.now(timezone.utc).strftime('%Y-%m-%d')})
# Run 'pulumi stack output ssh_config' to regenerate

Host k3s-bastion
  HostName {bastion_public}
  User ubuntu
  IdentityFile ~/MyKeyPair.pem
  StrictHostKeyChecking no

Host k3s-master
  HostName {master_private}
  User ubuntu
  IdentityFile ~/MyKeyPair.pem
  ProxyJump k3s-bastion
  StrictHostKeyChecking no

Host k3s-worker-1
  HostName {worker1_private}
  User ubuntu
  IdentityFile ~/MyKeyPair.pem
  ProxyJump k3s-bastion
  StrictHostKeyChecking no

Host k3s-worker-2
  HostName {worker2_private}
  User ubuntu
  IdentityFile ~/MyKeyPair.pem
  ProxyJump k3s-bastion
  StrictHostKeyChecking no
"""

# Export SSH config as a stack output
ssh_config = pulumi.Output.all(
    bastion_public=bastion_instance.public_ip,
    master_private=master_instance.private_ip,
    worker1_private=worker_instance_1.private_ip,
    worker2_private=worker_instance_2.private_ip,
).apply(lambda args: generate_ssh_config(
    args["bastion_public"],
    args["master_private"],
    args["worker1_private"],
    args["worker2_private"]
))

pulumi.export("ssh_config", ssh_config)

# Export update command for convenience
update_command = pulumi.Output.format(
    "pulumi stack output ssh_config >> ~/.ssh/config && chmod 600 ~/.ssh/config"
)
pulumi.export("ssh_config_update_command", update_command)
