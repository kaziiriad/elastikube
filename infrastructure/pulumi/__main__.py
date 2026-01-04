"""
K3s Autoscaler Infrastructure

This Pulumi program provisions AWS resources for the K3s autoscaler:
- DynamoDB tables for state management and WAL
- S3 bucket for K3s token storage
- Lambda function for autoscaling decisions
- EventBridge rule for triggering
- CloudWatch monitoring and alarms
- VPC and networking (optional, if cluster doesn't exist)
"""

import os
import pulumi
import pulumi_aws as aws
from pulumi_aws import ec2, lambda_, dynamodb, s3, iam

# =============================================================================
# Configuration
# =============================================================================
config = pulumi.Config()

# AWS Region
region = config.get("aws:region", "ap-southeast-1")

# K3s Configuration
cluster_name = config.get("k3s:clusterName", "production-k3s")
min_nodes = config.get_int("k3s:minNodes", 2)
max_nodes = config.get_int("k3s:maxNodes", 10)

# EC2 Configuration
master_instance_type = config.get("ec2:masterInstanceType", "t3.medium")
worker_instance_type = config.get("ec2:workerInstanceType", "t3.small")
# Ubuntu 22.04 AMI IDs (update if using different region)
ami_id = config.get("ec2:amiId", "ami-0f1e31d01140d0ae2")  # ap-southeast-1

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

# Security Group for K3s cluster traffic
security_group = aws.ec2.SecurityGroup("k3s-secgrp",
    description='Enable K3s cluster and monitoring access',
    vpc_id=vpc.id,
    ingress=[
        # SSH access (optional - for debugging, consider restricting CIDR)
        {
            "protocol": "tcp",
            "from_port": 22,
            "to_port": 22,
            "cidr_blocks": ["0.0.0.0/0"],
        },
        # Kubernetes API Server
        {
            "protocol": "tcp",
            "from_port": 6443,
            "to_port": 6443,
            "cidr_blocks": ["0.0.0.0/0"],
        },
        # Prometheus (for autoscaler access)
        {
            "protocol": "tcp",
            "from_port": 9090,
            "to_port": 9090,
            "cidr_blocks": ["0.0.0.0/0"],
        },
        # Prometheus NodePort (autoscaler queries this)
        {
            "protocol": "tcp",
            "from_port": 30900,
            "to_port": 30900,
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
        'Name': 'k3s-secgrp',
    }
)

# =============================================================================
# S3 Bucket for K3s Token and Scripts
# =============================================================================
k3s_bucket = s3.Bucket(
    "k3s-config-bucket",
    bucket=f"{cluster_name}-config",
    tags={**common_tags, "Name": "k3s-config-bucket"}
)

# Versioning for the bucket
k3s_bucket_versioning = s3.BucketVersioning(
    "k3s-config-bucket-versioning",
    bucket=k3s_bucket.id,
    versioning_configuration={
        "status": "Enabled"
    }
)

# Server-side encryption
k3s_bucket_encryption = s3.BucketServerSideEncryptionConfiguration(
    "k3s-config-bucket-encryption",
    bucket=k3s_bucket.id,
    rules=[{
        "apply_server_side_encryption_by_default": {
            "sse_algorithm": "AES256"
        }
    }]
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

# Custom policy for autoscaler permissions
autoscaler_policy = iam.RolePolicy(
    "k3s-autoscaler-lambda-policy",
    role=lambda_role.id,
    policy=pulumi.Output.all(
        cluster_state_arn=cluster_state_table.arn,
        wal_table_arn=wal_table.arn,
        bucket_arn=k3s_bucket.arn,
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
            # S3 Permissions
            {
                "actions": ["s3:GetObject"],
                "resources": [f"{args['bucket_arn']}/*"],
                "effect": "Allow",
            },
            # SSM Permissions
            {
                "actions": ["ssm:GetParameter"],
                "resources": [f"arn:aws:ssm:{region}:*:parameter/k3s/*"],
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
        bucket_arn=k3s_bucket.arn,
        cluster_state_arn=cluster_state_table.arn,
    ).apply(lambda args: iam.get_policy_document(
        statements=[
            {
                "actions": ["s3:GetObject"],
                "resources": [f"{args['bucket_arn']}/k3s-token"],
                "effect": "Allow",
            },
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

# Instance Profile for worker nodes
worker_instance_profile = iam.InstanceProfile(
    "k3s-worker-instance-profile",
    role=worker_role.name,
    tags={**common_tags, "Name": "k3s-worker-instance-profile"}
)

# =============================================================================
# EC2 Instances
# =============================================================================
# Note: These are initial seed instances. The autoscaler Lambda will
# dynamically add/remove worker nodes based on cluster metrics.

# SSH Key Pair (optional - set PUBLIC_KEY env var to enable)
public_key = os.getenv("PUBLIC_KEY")
key_pair = None
if public_key:
    key_pair = aws.ec2.KeyPair("MyKeyPair",
        key_name="MyKeyPair",
        public_key=public_key
    )

# Master Instance
master_instance = ec2.Instance(
    'master-instance',
    instance_type=master_instance_type,
    ami=ami_id,
    subnet_id=private_subnet.id,
    vpc_security_group_ids=[security_group.id],
    key_name=key_pair.key_name if key_pair else None,
    tags={**common_tags, 'Name': 'k3s-master', 'NodeRole': 'master'}
)

# Worker Instance 1 (permanent)
worker_instance_1 = ec2.Instance('worker-instance-1',
    instance_type=worker_instance_type,
    ami=ami_id,
    subnet_id=private_subnet.id,
    vpc_security_group_ids=[security_group.id],
    iam_instance_profile=worker_instance_profile.name,
    key_name=key_pair.key_name if key_pair else None,
    tags={**common_tags, 'Name': 'k3s-worker-1', 'NodeRole': 'worker', 'Permanent': 'true'}
)

# Worker Instance 2 (permanent)
worker_instance_2 = ec2.Instance('worker-instance-2',
    instance_type=worker_instance_type,
    ami=ami_id,
    subnet_id=private_subnet.id,
    vpc_security_group_ids=[security_group.id],
    iam_instance_profile=worker_instance_profile.name,
    key_name=key_pair.key_name if key_pair else None,
    tags={**common_tags, 'Name': 'k3s-worker-2', 'NodeRole': 'worker', 'Permanent': 'true'}
)

# =============================================================================
# Lambda Function
# =============================================================================

# Lambda Log Group
lambda_log_group = aws.cloudwatch.LogGroup(
    "k3s-autoscaler-log-group",
    name=f"/aws/lambda/k3s-autoscaler",
    retention_in_days=7,
    tags={**common_tags, "Name": "k3s-autoscaler-logs"}
)

# TODO: Create the Lambda function package
# The actual Lambda code will be in lambda/ directory
# For now, we'll create a placeholder that you'll update later

# Placeholder: You'll need to build and package the Lambda code
# pulumi.LoggedWarning("Lambda function placeholder - implement packaging in lambda/ directory")

# =============================================================================
# EventBridge Rule
# =============================================================================

# EventBridge Rule - triggers every 2 minutes
event_rule = aws.cloudwatch.EventRule(
    "k3s-autoscaler-schedule",
    schedule_expression="rate 2 minutes",
    tags={**common_tags, "Name": "k3s-autoscaler-schedule"}
)

# TODO: Add EventBridge target to invoke Lambda
# This will be added after Lambda function is created
# event_target = aws.cloudwatch.EventTarget("k3s-autoscaler-target",
#     rule=event_rule.name,
#     arn=lambda_function.arn,
# )

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
# Outputs
# =============================================================================
pulumi.export("cluster_name", cluster_name)
pulumi.export("dynamodb_cluster_state_table", cluster_state_table.name)
pulumi.export("dynamodb_wal_table", wal_table.name)
pulumi.export("s3_config_bucket", k3s_bucket.bucket)
pulumi.export("lambda_role_arn", lambda_role.arn)
pulumi.export("worker_instance_profile", worker_instance_profile.name)
pulumi.export("cloudwatch_log_group", lambda_log_group.name)
pulumi.export("event_rule_arn", event_rule.arn)

# Configuration outputs
pulumi.export("config_min_nodes", min_nodes)
pulumi.export("config_max_nodes", max_nodes)
pulumi.export("config_scale_up_threshold", scale_up_threshold)
pulumi.export("config_scale_down_threshold", scale_down_threshold)
pulumi.export("config_scale_up_cooldown", scale_up_cooldown)
pulumi.export("config_scale_down_cooldown", scale_down_cooldown)
