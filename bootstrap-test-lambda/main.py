"""Bootstrap Test Lambda handler.

This Lambda handles scale-up operations:
1. Fetch EC2 configuration from environment
2. Fetch bootstrap script from S3
3. Launch a new EC2 instance with the bootstrap script
4. Update DynamoDB state with new node count

Triggered by EventBridge when main autoscaler decides to scale up.
Also supports direct invocation for testing.

Environment variables required:
    SUBNET_ID: Subnet ID to launch instance in
    SECURITY_GROUP_ID: Security group ID
    IAM_INSTANCE_PROFILE: IAM instance profile name
    AMI_ID: AMI ID for the instance
    INSTANCE_TYPE: EC2 instance type
    USER_DATA_S3_BUCKET: S3 bucket containing bootstrap script
    USER_DATA_S3_KEY: S3 key for bootstrap script
    STATE_TABLE_NAME: DynamoDB table for cluster state
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: dict, context: Any) -> dict:
    """Lambda entry point for scale-up operations.

    Args:
        event: Lambda event
            - EventBridge format: {"detail-type": "ScaleUp", "detail": {...}}
            - Direct format: {"action": "launch", ...} (for testing)
        context: Lambda context

    Returns:
        Response with operation status
    """
    logger.info("Scale-up Lambda invoked")
    logger.info(f"Event: {json.dumps(event)}")

    try:
        # Handle EventBridge events
        detail_type = event.get("detail-type")

        if detail_type == "ScaleUp":
            # EventBridge event from main autoscaler
            detail = event.get("detail", {})
            logger.info(f"Scale-up event: {detail.get('reason')}")
            return _handle_scale_up(detail)
        elif detail_type:
            # Unknown EventBridge event
            return {
                "statusCode": 400,
                "body": json.dumps({"error": f"Unknown event type: {detail_type}"}),
            }

        # Handle direct invocation (for testing)
        action = event.get("action", "launch")

        if action == "launch":
            return _launch_test_instance()
        elif action == "describe":
            instance_id = event.get("instance_id")
            if not instance_id:
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "instance_id required for describe action"}),
                }
            return _describe_instance(instance_id)
        elif action == "terminate":
            instance_id = event.get("instance_id")
            if not instance_id:
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "instance_id required for terminate action"}),
                }
            return _terminate_instance(instance_id)
        else:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": f"Unknown action: {action}"}),
            }

    except Exception as e:
        logger.exception("Lambda execution failed")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": str(e),
            }),
        }


def _get_config() -> dict:
    """Get configuration from environment variables."""
    config = {
        "subnet_id": os.environ.get("SUBNET_ID"),
        "security_group_id": os.environ.get("SECURITY_GROUP_ID"),
        "iam_instance_profile": os.environ.get("IAM_INSTANCE_PROFILE"),
        "ami_id": os.environ.get("AMI_ID"),
        "instance_type": os.environ.get("INSTANCE_TYPE", "t3.small"),
        "key_name": os.environ.get("KEY_NAME"),  # SSH key pair for debugging
        "s3_bucket": os.environ.get("USER_DATA_S3_BUCKET"),
        "s3_key": os.environ.get("USER_DATA_S3_KEY"),
        "state_table_name": os.environ.get("STATE_TABLE_NAME"),
        "cluster_name": os.environ.get("CLUSTER_NAME", "production-k3s"),
    }

    # Validate required config (key_name and state_table_name are optional)
    optional_keys = ("state_table_name", "key_name")
    missing = [k for k, v in config.items() if not v and k not in optional_keys]
    if missing:
        raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")

    return config


def _handle_scale_up(detail: dict) -> dict:
    """Handle scale-up event from EventBridge.

    Launches a new worker instance and updates DynamoDB state.

    Args:
        detail: Event detail containing scaling decision

    Returns:
        Response with operation status
    """
    import boto3
    from botocore.exceptions import ClientError

    config = _get_config()
    current_nodes = detail.get("current_nodes", 0)
    target_nodes = detail.get("target_nodes", current_nodes + 1)
    reason = detail.get("reason", "")

    logger.info(f"Scale-up: {current_nodes} -> {target_nodes} nodes ({reason})")

    # Launch the new worker instance
    launch_result = _launch_test_instance()
    launch_status = launch_result.get("statusCode", 500)

    if launch_status == 200:
        body = json.loads(launch_result.get("body", "{}"))
        instance_id = body.get("instance_id")

        logger.info(f"Launched instance {instance_id}, updating DynamoDB state")

        # Update DynamoDB state with new node count
        if config.get("state_table_name"):
            try:
                dynamodb = boto3.client("dynamodb")
                new_node_count = current_nodes + 1

                dynamodb.update_item(
                    TableName=config["state_table_name"],
                    Key={"cluster_id": {"S": config["cluster_name"]}},
                    UpdateExpression=(
                        "SET node_count = :node_count, "
                        "scaling_in_progress = :false, "
                        "last_scale_operation = :op, "
                        "last_scale_time = :time"
                    ),
                    ExpressionAttributeValues={
                        ":node_count": {"N": str(new_node_count)},
                        ":false": {"S": "false"},
                        ":op": {"S": "SCALE_UP"},
                        ":time": {"S": datetime.now(timezone.utc).isoformat()},
                    },
                )

                logger.info(f"✓ State updated: node_count={new_node_count}")

            except ClientError as e:
                logger.error(f"Failed to update DynamoDB state: {e}")
                # Don't fail the operation - instance was launched successfully

        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "success",
                "instance_id": instance_id,
                "new_node_count": current_nodes + 1,
                "message": f"Scale-up initiated: instance {instance_id} launched",
            }),
        }
    else:
        logger.error(f"Failed to launch instance: {launch_result}")
        return launch_result


def _fetch_bootstrap_script(s3_client, bucket: str, key: str) -> str:
    """Fetch bootstrap script from S3 and log its content."""
    logger.info(f"Fetching bootstrap script from s3://{bucket}/{key}")
    response = s3_client.get_object(Bucket=bucket, Key=key)
    script = response["Body"].read().decode("utf-8")
    logger.info(f"Fetched bootstrap script ({len(script)} bytes)")

    # Log the bootstrap script content for verification
    logger.info("=" * 60)
    logger.info("BOOTSTRAP SCRIPT CONTENT:")
    logger.info(script)
    logger.info("=" * 60)

    return script


def _launch_test_instance() -> dict:
    """Launch a test EC2 instance with bootstrap script.

    Process:
    1. Fetch bootstrap script from S3
    2. Log bootstrap script content for verification
    3. Launch EC2 instance with bootstrap script as user-data
    """
    import base64

    logger.info("=" * 60)
    logger.info("STEP 1: Initializing test instance launch")
    logger.info("=" * 60)

    # Initialize AWS clients
    ec2_client = boto3.client("ec2")
    s3_client = boto3.client("s3")

    # Get configuration
    config = _get_config()
    logger.info(f"Config: subnet={config['subnet_id']}, ami={config['ami_id']}")

    logger.info("")
    logger.info("=" * 60)
    logger.info("STEP 2: Fetching bootstrap script from S3")
    logger.info("=" * 60)

    # Fetch bootstrap script from S3 (logs content in the function)
    bootstrap_script = _fetch_bootstrap_script(
        s3_client, config["s3_bucket"], config["s3_key"]
    )

    logger.info("")
    logger.info("=" * 60)
    logger.info("STEP 3: Launching EC2 instance with bootstrap script")
    logger.info("=" * 60)

    # Prepare tags
    test_id = uuid.uuid4().hex[:8]
    tags = [
        {"Key": "Name", "Value": f"k3s-bootstrap-test-{test_id}"},
        {"Key": "Project", "Value": "k3s-autoscaler"},
        {"Key": "Purpose", "Value": "bootstrap-test"},
        {"Key": "CreatedBy", "Value": "bootstrap-test-lambda"},
        {"Key": "LaunchTime", "Value": datetime.now(timezone.utc).isoformat()},
    ]

    # Prepare instance launch parameters
    user_data_base64 = base64.b64encode(
        bootstrap_script.encode("utf-8")
    ).decode("utf-8")

    run_params = {
        "ImageId": config["ami_id"],
        "InstanceType": config["instance_type"],
        "MinCount": 1,
        "MaxCount": 1,
        "SubnetId": config["subnet_id"],
        "SecurityGroupIds": [config["security_group_id"]],
        "IamInstanceProfile": {"Name": config["iam_instance_profile"]},
        "UserData": user_data_base64,
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": tags,
            }
        ],
        "ClientToken": str(uuid.uuid4()),  # Idempotency
    }

    # Add SSH key pair if configured (for debugging)
    if config.get("key_name"):
        run_params["KeyName"] = config["key_name"]
        logger.info(f"Using SSH key pair: {config['key_name']}")

    logger.info(f"Launching instance with: ami={run_params['ImageId']}, "
                f"type={run_params['InstanceType']}, subnet={run_params['SubnetId']}")
    logger.info(f"User-data size: {len(user_data_base64)} bytes (base64 encoded)")

    # Launch instance
    response = ec2_client.run_instances(**run_params)
    instance_id = response["Instances"][0]["InstanceId"]

    logger.info(f"Launched test instance: {instance_id}")
    logger.info("=" * 60)

    # Return response with bootstrap script preview
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Test instance launched successfully",
            "instance_id": instance_id,
            "test_id": test_id,
            "bootstrap_script_preview": bootstrap_script[:500] + "..." if len(bootstrap_script) > 500 else bootstrap_script,
            "bootstrap_script_size_bytes": len(bootstrap_script),
            "config": {
                "ami": config["ami_id"],
                "instance_type": config["instance_type"],
                "subnet": config["subnet_id"],
                "s3_bucket": config["s3_bucket"],
                "s3_key": config["s3_key"],
            },
            "tags": {tag["Key"]: tag["Value"] for tag in tags},
            "next_actions": {
                "describe": f"Invoke with action=describe,instance_id={instance_id}",
                "terminate": f"Invoke with action=terminate,instance_id={instance_id}",
            }
        }, indent=2),
    }


def _describe_instance(instance_id: str) -> dict:
    """Describe instance status and fetch console output."""
    logger.info(f"Describing instance: {instance_id}")

    ec2_client = boto3.client("ec2")

    # Get instance details
    response = ec2_client.describe_instances(InstanceIds=[instance_id])
    instance = response["Reservations"][0]["Instances"][0]

    state = instance["State"]
    private_ip = instance.get("PrivateIpAddress", "N/A")
    public_ip = instance.get("PublicIpAddress", "N/A")

    logger.info(f"Instance {instance_id}: state={state['Name']}, ip={private_ip}")

    # Try to fetch console output
    console_output = ""
    console_output_available = False
    if state["Name"] in ["running", "stopped", "terminated"]:
        try:
            console_response = ec2_client.get_console_output(InstanceId=instance_id)
            console_output = console_response.get("Output", "")
            console_output_available = bool(console_output)
            if console_output:
                logger.info(f"Fetched {len(console_output)} bytes of console output")
        except ClientError as e:
            console_output = f"Could not fetch console output: {e}"

    # Extract key bootstrap steps from console output
    bootstrap_progress = _parse_bootstrap_progress(console_output)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "instance_id": instance_id,
            "state": state,
            "private_ip": private_ip,
            "public_ip": public_ip,
            "launch_time": instance.get("LaunchTime"),
            "console_output_available": console_output_available,
            "console_output": console_output[:5000] if console_output else "",
            "console_output_truncated": len(console_output) > 5000 if console_output else False,
            "bootstrap_progress": bootstrap_progress,
        }, indent=2, default=str),
    }


def _parse_bootstrap_progress(console_output: str) -> dict:
    """Parse bootstrap script progress from console output."""
    progress = {
        "steps_completed": [],
        "current_step": None,
        "ssm_agent_installed": False,
        "k3s_installed": False,
        "errors": [],
    }

    if not console_output:
        return progress

    lines = console_output.split("\n")
    for line in lines:
        # Check for step indicators
        if "[1/7]" in line or "[2/7]" in line or "[3/7]" in line or \
           "[4/7]" in line or "[5/7]" in line or "[6/7]" in line or "[7/7]" in line:
            step = line.strip()
            if "Installing" in step or "Waiting" in step or "Getting" in step or "Fetching" in step:
                progress["steps_completed"].append(step)

        # Check for SSM Agent
        if "SSM Agent is running" in line or "amazon-ssm-agent" in line:
            progress["ssm_agent_installed"] = True

        # Check for K3s installation
        if "k3s is running" in line or "Installing k3s" in line:
            progress["k3s_installed"] = True

        # Check for errors
        if "error:" in line.lower() or "failed:" in line.lower() or "Error:" in line:
            progress["errors"].append(line.strip())

    # Determine current step
    if progress["steps_completed"]:
        progress["current_step"] = progress["steps_completed"][-1]

    return progress


def _terminate_instance(instance_id: str) -> dict:
    """Terminate test instance."""
    logger.info(f"Terminating instance: {instance_id}")

    ec2_client = boto3.client("ec2")

    try:
        ec2_client.terminate_instances(InstanceIds=[instance_id])
        logger.info(f"Terminated instance: {instance_id}")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": f"Instance {instance_id} terminated",
                "instance_id": instance_id,
            }),
        }
    except ClientError as e:
        logger.error(f"Failed to terminate instance: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": f"Failed to terminate instance: {e}",
            }),
        }
