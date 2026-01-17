"""K3s Autoscaler Scale-Up Lambda handler.

This Lambda handles scale-up operations:
1. Fetch EC2 configuration from environment
2. Fetch bootstrap script from S3
3. Launch a new EC2 instance with the bootstrap script
4. Update DynamoDB state with scale operation info

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
    CLUSTER_NAME: Name of the K3s cluster (default: production-k3s)
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


def _handle_health_check() -> dict:
    """Handle health check requests.

    Returns the health status of the scale-up Lambda.

    Returns:
        Health check response with component statuses
    """
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lambda": "scale-up-lambda",
        "components": {},
    }

    try:
        ec2_client = boto3.client("ec2")
        s3_client = boto3.client("s3")
        dynamodb_client = boto3.client("dynamodb")

        # Check EC2 connectivity
        try:
            ec2_client.describe_instances(MaxResults=1)
            health_status["components"]["ec2"] = {"status": "healthy"}
        except Exception as e:
            health_status["components"]["ec2"] = {"status": "unhealthy", "error": str(e)}
            health_status["status"] = "degraded"

        # Check S3 connectivity (bootstrap script)
        try:
            bucket = os.environ.get("USER_DATA_S3_BUCKET")
            key = os.environ.get("USER_DATA_S3_KEY", "user-data/worker-bootstrap.sh")
            if bucket:
                s3_client.head_object(Bucket=bucket, Key=key)
                health_status["components"]["s3"] = {
                    "status": "healthy",
                    "bucket": bucket,
                    "key": key,
                }
            else:
                health_status["components"]["s3"] = {"status": "unhealthy", "error": "Bucket not configured"}
                health_status["status"] = "degraded"
        except Exception as e:
            health_status["components"]["s3"] = {"status": "unhealthy", "error": str(e)}
            health_status["status"] = "degraded"

        # Check DynamoDB connectivity
        try:
            table_name = os.environ.get("STATE_TABLE_NAME")
            if table_name:
                dynamodb_client.describe_table(TableName=table_name)
                health_status["components"]["dynamodb"] = {
                    "status": "healthy",
                    "table": table_name,
                }
            else:
                health_status["components"]["dynamodb"] = {"status": "unhealthy", "error": "Table not configured"}
                health_status["status"] = "degraded"
        except Exception as e:
            health_status["components"]["dynamodb"] = {"status": "unhealthy", "error": str(e)}
            health_status["status"] = "degraded"

        # Check configuration
        health_status["components"]["config"] = {
            "status": "healthy" if all(
                os.environ.get(k)
                for k in ["SUBNET_ID", "SECURITY_GROUP_ID", "AMI_ID", "INSTANCE_TYPE"]
            ) else "degraded",
            "subnet_id": os.environ.get("SUBNET_ID"),
            "security_group": os.environ.get("SECURITY_GROUP_ID"),
            "ami_id": os.environ.get("AMI_ID"),
            "instance_type": os.environ.get("INSTANCE_TYPE"),
        }

    except Exception as e:
        health_status["status"] = "unhealthy"
        health_status["error"] = str(e)

    return {
        "statusCode": 200 if health_status["status"] in ("healthy", "degraded") else 503,
        "body": json.dumps(health_status, indent=2),
    }


def lambda_handler(event: dict, context: Any) -> dict:
    """Lambda entry point for scale-up operations.

    Args:
        event: Lambda event
            - {"action": "health_check"} for health check
            - EventBridge format: {"detail-type": "ScaleUp", "detail": {...}}
            - Direct format: {"action": "launch", ...} (for testing)
        context: Lambda context

    Returns:
        Response with operation status
    """
    # Health check endpoint
    if event.get("action") == "health_check":
        return _handle_health_check()

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
        "use_spot_instances": os.environ.get("USE_SPOT_INSTANCES", "false").lower() == "true",
        "bootstrap_timeout": int(os.environ.get("BOOTSTRAP_TIMEOUT_SECONDS", "180")),
    }

    # Validate required config (key_name and state_table_name are optional)
    optional_keys = ("state_table_name", "key_name")
    # Check for None (not set) specifically, not falsy values (False is valid for use_spot_instances)
    missing = [k for k, v in config.items() if v is None and k not in optional_keys]
    if missing:
        raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")

    return config


def _check_bootstrap_credentials(config: dict) -> tuple[bool, str]:
    """Check if bootstrap credentials are properly configured.

    Verifies that the SSM parameter (master IP) and Secrets Manager secret
    (join token) are not set to "PENDING", which indicates the cluster
    hasn't been properly initialized.

    Args:
        config: Configuration dictionary containing cluster_name and aws_region

    Returns:
        Tuple of (is_valid, error_message)
    """
    import boto3

    cluster_name = config.get("cluster_name", "production-k3s")
    region = config.get("aws_region", "ap-southeast-1")

    try:
        ssm_client = boto3.client("ssm", region_name=region)
        secrets_client = boto3.client("secretsmanager", region_name=region)

        # Check SSM parameter (master IP)
        ssm_param_name = f"/k3s/{cluster_name}/master-ip"
        try:
            ssm_response = ssm_client.get_parameter(Name=ssm_param_name)
            master_ip = ssm_response["Parameter"]["Value"]
            if master_ip == "PENDING":
                return False, f"SSM parameter {ssm_param_name} is PENDING"
            logger.info(f"✓ Master IP from SSM: {master_ip}")
        except ssm_client.exceptions.ParameterNotFound:
            return False, f"SSM parameter {ssm_param_name} not found"

        # Check Secrets Manager (join token)
        secret_name = f"k3s-{cluster_name}-join-token"
        try:
            secret_response = secrets_client.get_secret_value(SecretId=secret_name)
            join_token = secret_response["SecretString"]
            if join_token == "PENDING":
                return False, f"Secrets Manager secret {secret_name} is PENDING"
            logger.info(f"✓ Join token from Secrets Manager: {join_token[:20]}...")
        except secrets_client.exceptions.ResourceNotFoundException:
            return False, f"Secrets Manager secret {secret_name} not found"

        return True, ""

    except Exception as e:
        logger.error(f"Failed to check bootstrap credentials: {e}")
        return False, f"Failed to verify credentials: {str(e)}"


def _acquire_distributed_lock(table_name: str, cluster_name: str, timeout_seconds: int = 10) -> tuple[bool, str]:
    """Acquire distributed lock for scaling operation.

    Args:
        table_name: DynamoDB table name
        cluster_name: Cluster identifier
        timeout_seconds: Lock acquisition timeout

    Returns:
        Tuple of (acquired: bool, lock_id: str)
    """
    import boto3

    dynamodb = boto3.client("dynamodb")

    # Current time and lock expiry
    now = datetime.now(timezone.utc)
    lock_id = f"scale-up-{now.isoformat()}"
    lock_expiry = (now.timestamp() + timeout_seconds) * 1000  # Convert to milliseconds

    try:
        response = dynamodb.update_item(
            TableName=table_name,
            Key={"cluster_id": {"S": cluster_name}},
            UpdateExpression="SET scaling_lock_id = :lock_id, lock_expiry = :expiry",
            ConditionExpression="attribute_not_exists(scaling_lock_id) OR lock_expiry < :now",
            ExpressionAttributeValues={
                ":lock_id": {"S": lock_id},
                ":expiry": {"N": str(lock_expiry)},
                ":now": {"N": str(now.timestamp() * 1000)},
            },
            ReturnValues="UPDATED_NEW",
        )
        logger.info("✓ Acquired distributed lock for scale-up")
        return True, lock_id
    except dynamodb.exceptions.ConditionalCheckFailedException:
        logger.warning("Could not acquire lock - another scaling operation in progress")
        return False, ""
    except Exception as e:
        logger.error(f"Failed to acquire lock: {e}")
        return False, ""


def _release_distributed_lock(table_name: str, cluster_name: str, lock_id: str) -> None:
    """Release distributed lock after scaling operation.

    Args:
        table_name: DynamoDB table name
        cluster_name: Cluster identifier
        lock_id: Lock identifier to release
    """
    import boto3

    dynamodb = boto3.client("dynamodb")

    try:
        dynamodb.update_item(
            TableName=table_name,
            Key={"cluster_id": {"S": cluster_name}},
            UpdateExpression="REMOVE scaling_lock_id, lock_expiry",
            ConditionExpression="scaling_lock_id = :lock_id",
            ExpressionAttributeValues={
                ":lock_id": {"S": lock_id},
            },
        )
        logger.info("✓ Released distributed lock")
    except Exception as e:
        logger.error(f"Failed to release lock: {e}")


def _check_scaling_in_progress(table_name: str, cluster_name: str) -> tuple[bool, str]:
    """Check if a scale-up operation is already in progress.

    Uses a persistent scaling_in_progress flag that survives lock expiry.
    This provides an additional layer of idempotency beyond the distributed lock.

    Args:
        table_name: DynamoDB table name
        cluster_name: Cluster identifier

    Returns:
        Tuple of (in_progress: bool, message: str)
    """
    import boto3

    dynamodb = boto3.client("dynamodb")

    try:
        response = dynamodb.get_item(
            TableName=table_name,
            Key={"cluster_id": {"S": cluster_name}},
            ProjectionExpression="scaling_in_progress, scaling_in_progress_since",
        )

        if "Item" not in response:
            return False, ""

        item = response.get("Item", {})
        in_progress = item.get("scaling_in_progress", {}).get("S", "") == "true"

        if not in_progress:
            return False, ""

        # Check if the flag is stale (> 5 minutes old)
        since_str = item.get("scaling_in_progress_since", {}).get("S", "")
        if since_str:
            try:
                since = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
                age_seconds = (datetime.now(timezone.utc) - since).total_seconds()

                if age_seconds > 300:  # 5 minutes
                    logger.warning(f"Stale scaling_in_progress flag found (age: {age_seconds}s)")
                    return False, "stale_flag"

            except ValueError:
                logger.warning(f"Invalid scaling_in_progress_since timestamp: {since_str}")

        return True, "Scaling already in progress"

    except Exception as e:
        logger.error(f"Failed to check scaling_in_progress: {e}")
        return False, ""


def _set_scaling_in_progress(table_name: str, cluster_name: str, in_progress: bool) -> None:
    """Set or clear the scaling_in_progress flag.

    Args:
        table_name: DynamoDB table name
        cluster_name: Cluster identifier
        in_progress: True to set flag, False to clear it
    """
    import boto3

    dynamodb = boto3.client("dynamodb")

    try:
        if in_progress:
            dynamodb.update_item(
                TableName=table_name,
                Key={"cluster_id": {"S": cluster_name}},
                UpdateExpression=(
                    "SET scaling_in_progress = :true, "
                    "scaling_in_progress_since = :since"
                ),
                ExpressionAttributeValues={
                    ":true": {"S": "true"},
                    ":since": {"S": datetime.now(timezone.utc).isoformat()},
                },
            )
            logger.info("✓ Set scaling_in_progress flag")
        else:
            dynamodb.update_item(
                TableName=table_name,
                Key={"cluster_id": {"S": cluster_name}},
                UpdateExpression="REMOVE scaling_in_progress, scaling_in_progress_since",
            )
            logger.info("✓ Cleared scaling_in_progress flag")
    except Exception as e:
        logger.error(f"Failed to update scaling_in_progress flag: {e}")


def _check_recent_scale_up(table_name: str, cluster_name: str, cooldown_seconds: int = 180) -> tuple[bool, str]:
    """Check if a scale-up recently completed.

    Prevents rapid successive scale-ups by checking the last_scale_operation time.

    Args:
        table_name: DynamoDB table name
        cluster_name: Cluster identifier
        cooldown_seconds: Minimum time between scale-ups (default: 3 minutes)

    Returns:
        Tuple of (should_skip: bool, message: str)
    """
    import boto3

    dynamodb = boto3.client("dynamodb")

    try:
        response = dynamodb.get_item(
            TableName=table_name,
            Key={"cluster_id": {"S": cluster_name}},
            ProjectionExpression="last_scale_operation, last_scale_time",
        )

        if "Item" not in response:
            return False, ""

        item = response.get("Item", {})
        last_operation = item.get("last_scale_operation", {}).get("S", "")

        if last_operation != "SCALE_UP":
            return False, ""

        last_scale_time_str = item.get("last_scale_time", {}).get("S", "")
        if not last_scale_time_str:
            return False, ""

        try:
            last_scale_time = datetime.fromisoformat(last_scale_time_str.replace("Z", "+00:00"))
            time_since_scale = (datetime.now(timezone.utc) - last_scale_time).total_seconds()

            if time_since_scale < cooldown_seconds:
                remaining = cooldown_seconds - int(time_since_scale)
                logger.info(f"Recent scale-up detected {time_since_scale:.0f}s ago, cooldown: {remaining}s remaining")
                return True, f"Cooldown active ({remaining}s remaining)"

        except ValueError:
            logger.warning(f"Invalid last_scale_time timestamp: {last_scale_time_str}")

        return False, ""

    except Exception as e:
        logger.error(f"Failed to check recent scale-up: {e}")
        return False, ""


def _get_pending_instances(cluster_name: str, max_age_seconds: int = 300) -> list:
    """Get instances launched by autoscaler that haven't been verified yet.

    Args:
        cluster_name: Cluster name
        max_age_seconds: Maximum age to consider "pending" (default: 5 minutes)

    Returns:
        List of pending instance dictionaries
    """
    import boto3

    ec2_client = boto3.client("ec2")

    try:
        response = ec2_client.describe_instances(
            Filters=[
                {"Name": "tag:Cluster", "Values": [cluster_name]},
                {"Name": "tag:NodeRole", "Values": ["worker"]},
                {"Name": "tag:CreatedBy", "Values": ["autoscaler"]},
                {"Name": "instance-state-name", "Values": ["running", "pending"]},
            ]
        )

        pending = []
        now = datetime.now(timezone.utc)

        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                # Check if already verified
                tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
                if tags.get("JoinVerified") == "true":
                    continue

                # Check age
                launch_time = instance.get("LaunchTime")
                if launch_time:
                    # Ensure timezone-aware
                    if launch_time.tzinfo is None:
                        launch_time = launch_time.replace(tzinfo=timezone.utc)

                    age_seconds = (now - launch_time).total_seconds()
                    if age_seconds < max_age_seconds:
                        pending.append(instance)

        return pending

    except Exception as e:
        logger.error(f"Failed to get pending instances: {e}")
        return []


def _handle_scale_up(detail: dict) -> dict:
    """Handle scale-up event from EventBridge with idempotency.

    IDEMPOTENCY LAYERS:
    1. Cooldown check - prevents rapid successive scale-ups
    2. Scaling in progress check - persistent flag check
    3. Pending instances check - looks for recent unverified instances
    4. Distributed lock - prevents concurrent execution

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

    # Step 1: Check bootstrap credentials (SSM + Secrets Manager)
    credentials_valid, credentials_error = _check_bootstrap_credentials(config)
    if not credentials_valid:
        logger.warning(f"Bootstrap credentials not ready: {credentials_error}")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "skipped",
                "reason": f"Bootstrap credentials not ready: {credentials_error}",
                "message": "Scale-up skipped - cluster not fully initialized",
            }),
        }

    # Step 2: Idempotency checks
    table_name = config.get("state_table_name")
    cluster_name = config.get("cluster_name", "production-k3s")

    if table_name:
        # Check 2a: Cooldown - prevent rapid successive scale-ups
        should_skip, skip_reason = _check_recent_scale_up(table_name, cluster_name, cooldown_seconds=180)
        if should_skip:
            logger.info(f"Scale-up skipped: {skip_reason}")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "skipped",
                    "reason": skip_reason,
                }),
            }

        # Check 2b: Scaling in progress - persistent flag
        in_progress, progress_reason = _check_scaling_in_progress(table_name, cluster_name)
        if in_progress:
            if progress_reason != "stale_flag":
                logger.info(f"Scale-up skipped: {progress_reason}")
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "status": "skipped",
                        "reason": progress_reason,
                    }),
                }
            else:
                # Clear stale flag and continue
                logger.info("Clearing stale scaling_in_progress flag")
                _set_scaling_in_progress(table_name, cluster_name, False)

        # Check 2c: Pending instances - look for recent unverified instances
        pending = _get_pending_instances(cluster_name, max_age_seconds=300)
        if pending:
            logger.info(f"Found {len(pending)} pending instances: {[i['InstanceId'] for i in pending]}")
            logger.info("Skipping new launch - waiting for pending instances to join")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "skipped",
                    "reason": f"Waiting for {len(pending)} pending instance(s) to join",
                    "pending_instances": [i["InstanceId"] for i in pending],
                }),
            }

    # Step 3: Acquire distributed lock with longer timeout (200s for full workflow)
    lock_id = ""
    lock_timeout = 200  # Increased from 10s to cover launch + verification

    if table_name:
        lock_acquired, lock_id = _acquire_distributed_lock(
            table_name, cluster_name, timeout_seconds=lock_timeout
        )
        if not lock_acquired:
            logger.warning("Could not acquire distributed lock - another scaling operation in progress")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "skipped",
                    "reason": "Distributed lock not available - another scaling operation in progress",
                }),
            }

        # Set scaling_in_progress flag
        _set_scaling_in_progress(table_name, cluster_name, True)

    # Step 4: Launch the new worker instance
    try:
        launch_result = _launch_test_instance()
        launch_status = launch_result.get("statusCode", 500)

        if launch_status == 200:
            body = json.loads(launch_result.get("body", "{}"))
            instance_id = body.get("instance_id")

            logger.info(f"Launched instance {instance_id}, waiting for bootstrap...")

            # Verification: Wait for bootstrap script to set JoinStatus=success tag
            # Use configured timeout (default 180s / 3 minutes)
            bootstrap_timeout = config.get("bootstrap_timeout", 180)
            verify_result = _verify_node_joined_tag(instance_id, timeout_seconds=bootstrap_timeout)

            # Log verification result
            if verify_result["success"]:
                logger.info("✓ Node successfully joined cluster")
            else:
                logger.warning(f"Bootstrap verification: {verify_result.get('error')}")

            # Update DynamoDB state
            if table_name:
                try:
                    dynamodb = boto3.client("dynamodb")
                    dynamodb.update_item(
                        TableName=table_name,
                        Key={"cluster_id": {"S": cluster_name}},
                        UpdateExpression=(
                            "SET last_scale_operation = :op, "
                            "last_scale_time = :time, "
                            "last_launched_instance = :instance_id"
                        ),
                        ExpressionAttributeValues={
                            ":op": {"S": "SCALE_UP"},
                            ":time": {"S": datetime.now(timezone.utc).isoformat()},
                            ":instance_id": {"S": instance_id},
                        },
                    )
                    logger.info("✓ State updated: scale operation logged")
                except ClientError as e:
                    logger.error(f"Failed to update DynamoDB state: {e}")

            logger.info("=" * 60)
            logger.info("✓ SCALE-UP OPERATION COMPLETED")
            logger.info(f"  Instance ID: {instance_id}")
            logger.info(f"  Instance IP: {verify_result.get('private_ip', 'N/A')}")
            logger.info(f"  K3s API: {verify_result.get('api_status', 'unknown')}")
            logger.info(f"  Cluster: {cluster_name}")
            logger.info("=" * 60)

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "success",
                    "instance_id": instance_id,
                    "private_ip": verify_result.get("private_ip"),
                    "api_status": verify_result.get("api_status"),
                    "message": f"Scale-up completed: instance {instance_id} launched",
                }),
            }
        else:
            logger.error(f"Failed to launch instance: {launch_result}")
            return launch_result
    finally:
        # Step 5: Always cleanup
        if table_name:
            # Clear scaling_in_progress flag
            _set_scaling_in_progress(table_name, cluster_name, False)
            # Release distributed lock
            if lock_id:
                _release_distributed_lock(table_name, cluster_name, lock_id)


def _verify_node_joined_tag(instance_id: str, timeout_seconds: int = 180) -> dict:
    """Verify node joined by checking JoinStatus tag set by bootstrap script.

    The bootstrap script (user-data.sh.j2) tags the instance with:
    - JoinStatus=success when K3s agent is running
    - JoinStatus=failed when K3s agent fails to start

    This is much simpler than using kubectl - just check the tag!

    Args:
        instance_id: EC2 instance ID
        timeout_seconds: Maximum time to wait (default: 2 minutes)

    Returns:
        Dict with success, private_ip, api_status
    """
    import time

    logger.info(f"Waiting for bootstrap to complete (timeout={timeout_seconds}s)...")

    ec2_client = boto3.client("ec2")

    # Get initial instance info
    try:
        resp = ec2_client.describe_instances(InstanceIds=[instance_id])
        instance = resp["Reservations"][0]["Instances"][0]
        private_ip = instance.get("PrivateIpAddress", "")
        logger.info(f"Worker IP: {private_ip}")
    except (ClientError, IndexError, KeyError) as e:
        return {
            "success": False,
            "error": f"Failed to get instance info: {e}",
            "private_ip": None,
            "api_status": "unknown",
        }

    # Poll for JoinStatus tag every 10 seconds
    start_time = time.time()
    check_interval = 10

    while time.time() - start_time < timeout_seconds:
        try:
            resp = ec2_client.describe_instances(InstanceIds=[instance_id])
            instance = resp["Reservations"][0]["Instances"][0]
            tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}

            join_status = tags.get("JoinStatus", "")

            if join_status == "success":
                logger.info("✓ Bootstrap completed successfully")
                # Tag as verified for cleanup Lambda
                try:
                    ec2_client.create_tags(
                        Resources=[instance_id],
                        Tags=[
                            {"Key": "JoinVerified", "Value": "true"},
                            {"Key": "VerifiedAt", "Value": datetime.now(timezone.utc).isoformat()},
                        ]
                    )
                    logger.info(f"✓ Tagged {instance_id} as verified")
                except Exception as e:
                    logger.warning(f"Failed to tag as verified: {e}")

                return {
                    "success": True,
                    "private_ip": private_ip,
                    "api_status": "ready",
                }
            elif join_status == "failed":
                logger.error(f"Bootstrap failed for {instance_id}")
                return {
                    "success": False,
                    "error": "Bootstrap script reported failure",
                    "private_ip": private_ip,
                    "api_status": "failed",
                }
            else:
                # No status yet - keep waiting
                elapsed = int(time.time() - start_time)
                logger.info(f"Bootstrap in progress... (elapsed: {elapsed}s)")
                time.sleep(check_interval)
                continue

        except ClientError as e:
            logger.warning(f"Failed to check instance tags: {e}")
            time.sleep(check_interval)

    # Timeout - but don't fail, let cleanup Lambda handle it
    logger.warning(f"Bootstrap verification timed out after {timeout_seconds}s")
    return {
        "success": False,
        "error": f"Bootstrap verification timeout ({timeout_seconds}s)",
        "private_ip": private_ip,
        "api_status": "timeout",
    }


def _fetch_bootstrap_script(s3_client, bucket: str, key: str) -> str:
    """Fetch bootstrap script from S3 and log its content."""
    logger.info(f"Fetching bootstrap script from s3://{bucket}/{key}")
    response = s3_client.get_object(Bucket=bucket, Key=key)
    script = response["Body"].read().decode("utf-8")
    logger.info(f"Fetched bootstrap script ({len(script)} bytes)")

    # Log the bootstrap script content for verification
    # logger.info("=" * 60)
    # logger.info("BOOTSTRAP SCRIPT CONTENT:")
    # logger.info(script)
    # logger.info("=" * 60)

    return script


def _launch_test_instance() -> dict:
    """Launch a worker EC2 instance with bootstrap script.

    Process:
    1. Fetch bootstrap script from S3
    2. Log bootstrap script content for verification
    3. Launch EC2 instance with bootstrap script as user-data
    """
    import base64

    logger.info("=" * 60)
    logger.info("STEP 1: Initializing worker instance launch")
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

    # Prepare tags - include Cluster and NodeRole for scale-down Lambda discovery
    cluster_name = config.get("cluster_name", "production-k3s")
    instance_uuid = uuid.uuid4().hex[:8]
    tags = [
        {"Key": "Name", "Value": f"k3s-worker-{cluster_name}-{instance_uuid}"},
        {"Key": "Cluster", "Value": cluster_name},
        {"Key": "NodeRole", "Value": "worker"},
        {"Key": "Permanent", "Value": "false"},
        {"Key": "CreatedBy", "Value": "autoscaler"},
        {"Key": "Project", "Value": "k3s-autoscaler"},
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

    # Add Spot instance configuration if enabled
    if config.get("use_spot_instances"):
        run_params["InstanceMarketOptions"] = {
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "one-time",
                "InstanceInterruptionBehavior": "terminate",
            }
        }
        # Add spot-specific tag
        tags.append({"Key": "InstanceLifecycle", "Value": "spot"})
        logger.info("Using SPOT instances (70-90% cost savings)")
        logger.info("Note: Spot instances may be interrupted by AWS")
    else:
        tags.append({"Key": "InstanceLifecycle", "Value": "on-demand"})
        logger.info("Using ON-DEMAND instances")

    logger.info(f"Launching instance with: ami={run_params['ImageId']}, "
                f"type={run_params['InstanceType']}, subnet={run_params['Subnet_id']}")
    logger.info(f"User-data size: {len(user_data_base64)} bytes (base64 encoded)")

    # Launch instance with spot fallback mechanism
    response = None
    instance_type = run_params["InstanceType"]
    using_spot = config.get("use_spot_instances")

    try:
        if using_spot:
            logger.info("Attempting SPOT instance launch (70-90% cost savings)")
            try:
                response = ec2_client.run_instances(**run_params)
                instance_id = response["Instances"][0]["InstanceId"]
                logger.info(f"✓ Launched SPOT instance: {instance_id}")
            except ec2_client.exceptions.ClientError as e:
                error_code = e.response["Error"]["Code"]
                error_message = e.response["Error"]["Message"]

                # Spot-specific errors that should trigger fallback
                spot_errors = [
                    "InsufficientInstanceCapacity",
                    "SpotInstanceCapacityNotAvailable",
                    "MaxSpotInstanceCountExceeded",
                ]

                if any(err in error_message for err in spot_errors):
                    logger.warning(f"⚠️ Spot capacity unavailable: {error_code} - {error_message}")
                    logger.info("Falling back to ON-DEMAND instance (full price)")

                    # Remove spot configuration and retry
                    run_params_ondemand = {k: v for k, v in run_params.items()
                                              if k not in ["InstanceMarketOptions", "SpotOptions"]}

                    # Update tags to reflect on-demand
                    for tags_spec in run_params_ondemand.get("TagSpecifications", []):
                        for tag_list in tags_spec.get("Tags", []):
                            if tag_list.get("Key") == "InstanceLifecycle":
                                tag_list["Value"] = "on-demand"

                    # Launch on-demand
                    response = ec2_client.run_instances(**run_params_ondemand)
                    instance_id = response["Instances"][0]["InstanceId"]
                    logger.info(f"✓ Launched ON-DEMAND instance: {instance_id}")
                else:
                    # Not a spot-related error, re-raise
                    raise
        else:
            logger.info("Launching ON-DEMAND instance (full price)")
            response = ec2_client.run_instances(**run_params)
            instance_id = response["Instances"][0]["InstanceId"]
            logger.info(f"✓ Launched ON-DEMAND instance: {instance_id}")

    except Exception as e:
        logger.error(f"Failed to launch instance: {e}")
        # Re-raise for calling code to handle
        raise

    # Ensure we got a response
    if response is None:
        raise RuntimeError("Failed to launch instance: no response from EC2 API")

    instance_id = response["Instances"][0]["InstanceId"]

    logger.info(f"Launched worker instance: {instance_id}")
    logger.info("=" * 60)

    # Return response with bootstrap script preview
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Worker instance launched successfully",
            "instance_id": instance_id,
            "instance_uuid": instance_uuid,
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


def _wait_for_instance_running(instance_id: str, timeout_seconds: int = 180) -> dict:
    """Wait for EC2 instance to be in running state.

    Args:
        instance_id: EC2 instance ID
        timeout_seconds: Maximum time to wait (default: 3 minutes)

    Returns:
        Dict with success status and instance details
    """
    import time
    ec2_client = boto3.client("ec2")

    logger.info(f"Waiting for instance {instance_id} to be running (timeout={timeout_seconds}s)...")

    start_time = time.time()
    check_interval = 5  # Check every 5 seconds

    while time.time() - start_time < timeout_seconds:
        try:
            response = ec2_client.describe_instances(InstanceIds=[instance_id])
            instance = response["Reservations"][0]["Instances"][0]
            state = instance["State"]["Name"]

            if state == "running":
                logger.info(f"✓ Instance {instance_id} is running")
                return {
                    "success": True,
                    "instance_id": instance_id,
                    "state": state,
                    "private_ip": instance.get("PrivateIpAddress"),
                    "public_ip": instance.get("PublicIpAddress"),
                }
            elif state in ["shutting-down", "terminated"]:
                return {
                    "success": False,
                    "instance_id": instance_id,
                    "state": state,
                    "error": f"Instance is {state}, cannot proceed",
                }

            logger.debug(f"Instance state: {state} (waiting...)")
            time.sleep(check_interval)

        except ClientError as e:
            logger.warning(f"Error checking instance state: {e}")
            time.sleep(check_interval)

    return {
        "success": False,
        "instance_id": instance_id,
        "error": f"Timeout waiting for instance to be running ({timeout_seconds}s)",
    }


def _verify_k3s_node_joined(instance_id: str, timeout_seconds: int = 180) -> dict:
    """Verify K3s agent joined the cluster successfully via SSM.

    Uses the master node's kubectl to verify the worker node joined correctly.

    Args:
        instance_id: EC2 instance ID
        timeout_seconds: Maximum time to wait (default: 3 minutes)

    Returns:
        Dict with verification status and node details
    """
    import time
    ssm_client = boto3.client("ssm")
    ec2_client = boto3.client("ec2")

    logger.info(f"Verifying K3s node join for {instance_id} (timeout={timeout_seconds}s)...")

    # Get cluster name
    cluster_name = os.environ.get("CLUSTER_NAME", "production-k3s")

    try:
        # Get master IP from SSM
        master_ip_response = ssm_client.get_parameter(
            Name=f"/k3s/{cluster_name}/master-ip"
        )
        master_ip = master_ip_response["Parameter"]["Value"]
    except ClientError as e:
        return {
            "success": False,
            "instance_id": instance_id,
            "error": f"Failed to get master IP from SSM: {e}",
        }

    # Look up master instance ID from master IP
    try:
        master_response = ec2_client.describe_instances(
            Filters=[{"Name": "private-ip-address", "Values": [master_ip]}]
        )
        master_instance_id = master_response["Reservations"][0]["Instances"][0]["InstanceId"]
    except (ClientError, IndexError, KeyError) as e:
        return {
            "success": False,
            "instance_id": instance_id,
            "error": f"Failed to find master instance for IP {master_ip}: {e}",
        }

    # Get the new worker's private IP to help identify it
    try:
        instance_info = ec2_client.describe_instances(InstanceIds=[instance_id])
        worker_private_ip = instance_info["Reservations"][0]["Instances"][0].get("PrivateIpAddress")
        if not worker_private_ip:
            return {
                "success": False,
                "instance_id": instance_id,
                "error": "Instance has no private IP yet",
            }
    except (ClientError, KeyError) as e:
        return {
            "success": False,
            "instance_id": instance_id,
            "error": f"Failed to get instance info: {e}",
        }

    logger.info(f"Worker IP: {worker_private_ip}, Master: {master_instance_id}")

    # Simple, step-by-step verification using master's kubectl
    # This avoids complex bash one-liners and f-string escaping issues
    check_command = f"""
        set -e
        WORKER_IP="{worker_private_ip}"

        echo "Checking for node with IP: $WORKER_IP"

        # Get all node names
        NODES=$(sudo kubectl get nodes -o custom-columns=NAME:.metadata.name --no-headers 2>/dev/null || true)
        echo "Found nodes:"
        echo "$NODES"

        # Find node by checking each node's internal IP
        FOUND_NODE=""
        for NODE in $NODES; do
            # Get node's internal IP address
            NODE_IP=$(sudo kubectl get node "$NODE" -o jsonpath='{{{{.status.addresses[?(@.type=="InternalIP")].address}}}}' 2>/dev/null || echo "")

            if [ "$NODE_IP" = "$WORKER_IP" ]; then
                echo "Found matching node: $NODE with IP: $NODE_IP"
                FOUND_NODE="$NODE"
                break
            fi
        done

        if [ -z "$FOUND_NODE" ]; then
            echo "NODE_STATUS=NotRegistered"
            echo "NODE_NAME=unknown"
            echo "JOIN_STATUS=not_found"
            exit 0
        fi

        echo "NODE_NAME=$FOUND_NODE"

        # Check if node is Ready
        READY_CONDITION=$(sudo kubectl get node "$FOUND_NODE" -o jsonpath='{{{{.status.conditions[?(@.type=="Ready")].status}}}}' 2>/dev/null || echo "False")
        NODE_STATUS=$(sudo kubectl get node "$FOUND_NODE" -o jsonpath='{{{{.status.phase}}}}' 2>/dev/null || echo "Unknown")

        echo "NODE_STATUS=$NODE_STATUS"
        echo "NODE_READY_CONDITION=$READY_CONDITION"

        if [ "$READY_CONDITION" = "True" ]; then
            echo "JOIN_STATUS=success"
        else
            echo "JOIN_STATUS=waiting"
        fi
    """

    start_time = time.time()
    check_interval = 15  # Check every 15 seconds

    while time.time() - start_time < timeout_seconds:
        try:
            result = ssm_client.send_command(
                InstanceIds=[master_instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [check_command]},
                TimeoutSeconds=30,
            )

            command_id = result["Command"]["CommandId"]

            # Wait for command to complete
            time.sleep(10)

            # Poll for command result
            max_poll_attempts = 4  # 40 seconds total
            for _ in range(max_poll_attempts):
                try:
                    invocation = ssm_client.get_command_invocation(
                        CommandId=command_id,
                        InstanceId=master_instance_id,
                    )

                    if invocation["Status"] in ["Success", "Failed", "TimedOut", "Cancelled"]:
                        break
                except ClientError:
                    pass
                time.sleep(10)

            # Get final result
            invocation = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=master_instance_id,
            )

            if invocation["Status"] == "Success":
                stdout = invocation.get("StandardOutputContent", "")

                # Parse key-value pairs from output
                result_dict = {}
                for line in stdout.split("\n"):
                    if "=" in line:
                        key, value = line.split("=", 1)
                        result_dict[key.strip()] = value.strip()

                node_status = result_dict.get("NODE_STATUS", "Unknown")
                join_status = result_dict.get("JOIN_STATUS", "unknown")
                node_name = result_dict.get("NODE_NAME", "unknown")
                ready_condition = result_dict.get("NODE_READY_CONDITION", "")

                if join_status == "success" and ready_condition == "True":
                    logger.info(f"✓ Node {node_name} successfully joined and is Ready")
                    return {
                        "success": True,
                        "instance_id": instance_id,
                        "node_name": node_name,
                        "node_status": node_status,
                        "ready_condition": ready_condition,
                        "agent_status": "running",
                    }
                elif node_status == "NotRegistered" or node_status == "not_found":
                    time.sleep(check_interval)
                    continue
                else:
                    time.sleep(check_interval)
                    continue

        except ClientError as e:
            logger.warning(f"SSM check failed: {e}")
            time.sleep(check_interval)

    # Timeout reached - return partial success with verification needed
    # The instance was launched successfully, but we couldn't verify it joined
    # The decision Lambda will reconcile on next run
    logger.warning(f"Timeout waiting for node to join ({timeout_seconds}s)")
    return {
        "success": False,
        "instance_id": instance_id,
        "error": f"Timeout waiting for K3s node to join ({timeout_seconds}s)",
        "verification_needed": True,
        "worker_ip": worker_private_ip,
    }


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
