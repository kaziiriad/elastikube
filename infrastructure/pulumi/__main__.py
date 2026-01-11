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
import pulumi_aws as aws
from pulumi_aws import ec2, lambda_, dynamodb, iam, ssm, secretsmanager, s3

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
vpc = ec2.Vpc(
    'my-vpc',
    cidr_block='10.0.0.0/16',
    enable_dns_hostnames=True,
    enable_dns_support=True,
    tags={
        'Name': 'my-vpc',
    }
)

# Create subnets
public_subnet = ec2.Subnet('public-subnet',
    vpc_id=vpc.id,
    cidr_block='10.0.1.0/24',
    map_public_ip_on_launch=True,
    availability_zone='ap-southeast-1a',
    tags={
        'Name': 'public-subnet',
    }
)

private_subnet = ec2.Subnet('private-subnet',
    vpc_id=vpc.id,
    cidr_block='10.0.2.0/24',
    map_public_ip_on_launch=False,
    availability_zone='ap-southeast-1a',
    tags={
        'Name': 'private-subnet',
    }
)

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
    route_table_id=public_route_table.id
)

# Elastic IP for NAT Gateway
eip = ec2.Eip(
    'nat-eip',
    tags={'Name': 'k3s-deployment-eip'}
)

# NAT Gateway
nat_gateway = ec2.NatGateway(
    'nat-gateway',
    subnet_id=public_subnet.id,
    allocation_id=eip.id,
    tags={
        'Name': 'nat-gateway',
    }
)

# Route Table for Private Subnet 
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

# Associate the private route table with the private subnet
private_route_table_association = ec2.RouteTableAssociation(
    'private-route-table-association',
    subnet_id=private_subnet.id,
    route_table_id=private_route_table.id
)

# Security Group for Bastion Host (SSH access from internet)
bastion_security_group = aws.ec2.SecurityGroup("bastion-secgrp",
    description='Enable SSH access to bastion host',
    vpc_id=vpc.id,
    ingress=[
        # SSH access from internet (restrict to your IP in production)
        {
            "protocol": "tcp",
            "from_port": 22,
            "to_port": 22,
            "cidr_blocks": ["0.0.0.0/0"],
        },
    ],
    egress=[{
        "protocol": "-1",
        "from_port": 0,
        "to_port": 0,
        "cidr_blocks": ["0.0.0.0/0"],
    }],
    tags={
        'Name': 'bastion-secgrp',
    }
)

# Security Group for K3s cluster traffic (private subnet)
security_group = aws.ec2.SecurityGroup("k3s-secgrp",
    description='Enable K3s cluster and monitoring access',
    vpc_id=vpc.id,
    ingress=[
        # SSH access ONLY from bastion host
        {
            "protocol": "tcp",
            "from_port": 22,
            "to_port": 22,
            "security_groups": [bastion_security_group.id],
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
        # K3s supervisor API
        {
            "protocol": "tcp",
            "from_port": 6443,
            "to_port": 6443,
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
        'Name': 'k3s-secgrp',
    }
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
master_ip_parameter = ssm.Parameter(
    "k3s-master-ip",
    name=f"/k3s/{cluster_name}/master-ip",
    type="String",
    value="PENDING",  # Will be populated by Ansible after cluster setup
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
    tags={**common_tags, "Name": "k3s-autoscaler-lambda-role"}
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
                "actions": ["ssm:SendCommand", "ssm:GetCommandInvocation"],
                "resources": [
                    f"arn:aws:ssm:{region}:*:document/AWS-RunShellScript",
                    f"arn:aws:ec2:{region}:*:instance/*",
                ],
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
    tags={**common_tags, "Name": "k3s-worker-node-role"}
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

# =============================================================================
# Master Node IAM Role (for SSM access - kubectl drain)
# =============================================================================
master_role = iam.Role(
    "k3s-master-node-role",
    assume_role_policy=ec2_assume_role.json,
    tags={**common_tags, "Name": "k3s-master-node-role"}
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
    vpc_security_group_ids=[security_group.id],
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
    vpc_security_group_ids=[security_group.id],
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
    vpc_security_group_ids=[security_group.id],
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

# Lambda Log Group
lambda_log_group = aws.cloudwatch.LogGroup(
    "k3s-autoscaler-log-group",
    name=f"/aws/lambda/k3s-autoscaler",
    retention_in_days=7,
    tags={**common_tags, "Name": "k3s-autoscaler-log-group"},
)

# Lambda deployment package
# Build the Lambda package first: cd lambda && ./build.sh
lambda_archive = pulumi.FileArchive(f"{os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}/lambda/build/lambda.zip")

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
        security_group_ids=[security_group.id],
    ),
    environment=lambda_.FunctionEnvironmentArgs(
        variables={
            "CLUSTER_NAME": cluster_name,
            "PROMETHEUS_URL": master_instance.private_ip.apply(lambda ip: f"http://{ip}:30900"),
            "STATE_TABLE_NAME": cluster_state_table.name,
            "WAL_TABLE_NAME": wal_table.name,
            "MIN_NODES": str(min_nodes),
            "MAX_NODES": str(max_nodes),
            "SCALE_UP_THRESHOLD": str(scale_up_threshold),
            "SCALE_DOWN_THRESHOLD": str(scale_down_threshold),
            "SCALE_UP_COOLDOWN": str(scale_up_cooldown),
            "SCALE_DOWN_COOLDOWN": str(scale_down_cooldown),
            "DRY_RUN": "false",
            # EC2 Configuration
            "SUBNET_ID": private_subnet.id,
            "SECURITY_GROUP_ID": security_group.id,
            "IAM_INSTANCE_PROFILE": worker_instance_profile.name,
            "AMI_ID": ami_id,
            "INSTANCE_TYPE": worker_instance_type,
            # S3 Configuration for worker bootstrap script
            "USER_DATA_S3_BUCKET": worker_userdata_bucket.bucket,
            "USER_DATA_S3_KEY": "user-data/worker-bootstrap.sh",
        }
    ),
    code=lambda_archive,
    tags={**common_tags, "Name": "k3s-autoscaler"}
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
# CloudWatch Dashboard
# =============================================================================

# Read dashboard definition from JSON file
dashboard_path = pathlib.Path(__file__).parent.parent.parent / "monitoring" / "dashboards" / "k3s-autoscaler-dashboard.json"

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

autoscaler_dashboard = aws.cloudwatch.Dashboard(
    "k3s-autoscaler-dashboard",
    dashboard_name="K3s-Autoscaler-Metrics",
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
pulumi.export("worker_instance_profile", worker_instance_profile.name)
pulumi.export("master_instance_profile", master_instance_profile.name)
pulumi.export("cloudwatch_log_group", lambda_log_group.name)
pulumi.export("event_rule_arn", event_rule.arn)
pulumi.export("cloudwatch_dashboard", autoscaler_dashboard.dashboard_name)

# Security Group IDs
pulumi.export("security_group_id", security_group.id)
pulumi.export("bastion_security_group_id", bastion_security_group.id)

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
