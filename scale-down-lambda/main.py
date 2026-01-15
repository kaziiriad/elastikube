"""Worker Cleanup Lambda handler.

This Lambda handles worker node operations:
- List all worker instances
- Describe worker details
- Drain a worker node (kubectl drain via SSM)
- Terminate a worker instance
- Scale down (selects non-permanent worker using LIFO)

Environment variables required:
    CLUSTER_NAME: Name of the K3s cluster
    SECURITY_GROUP_ID: Security group ID for filtering
    SUBNET_ID: Subnet ID for filtering
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _handle_health_check() -> dict:
    """Handle health check requests.

    Returns the health status of the scale-down Lambda.

    Returns:
        Health check response with component statuses
    """
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lambda": "scale-down-lambda",
        "components": {},
    }

    try:
        ec2_client = boto3.client("ec2")
        ssm_client = boto3.client("ssm")
        dynamodb_client = boto3.client("dynamodb")

        # Check EC2 connectivity
        try:
            ec2_client.describe_instances(MaxResults=1)
            health_status["components"]["ec2"] = {"status": "healthy"}
        except Exception as e:
            health_status["components"]["ec2"] = {"status": "unhealthy", "error": str(e)}
            health_status["status"] = "degraded"

        # Check SSM connectivity (for kubectl drain)
        try:
            ssm_client.get_connection_status(NextToken="", MaxResults=1)
            health_status["components"]["ssm"] = {"status": "healthy"}
        except Exception as e:
            health_status["components"]["ssm"] = {"status": "unhealthy", "error": str(e)}
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
        cluster_name = os.environ.get("CLUSTER_NAME")
        health_status["components"]["config"] = {
            "status": "healthy" if cluster_name else "degraded",
            "cluster_name": cluster_name,
            "security_group": os.environ.get("SECURITY_GROUP_ID"),
            "subnet_id": os.environ.get("SUBNET_ID"),
        }

    except Exception as e:
        health_status["status"] = "unhealthy"
        health_status["error"] = str(e)

    return {
        "statusCode": 200 if health_status["status"] in ("healthy", "degraded") else 503,
        "body": json.dumps(health_status, indent=2),
    }


def lambda_handler(event: dict, context: Any) -> dict:
    """Lambda entry point for worker cleanup operations.

    Args:
        event: Lambda event
            - {"action": "health_check"} for health check
            - EventBridge format: {"detail-type": "ScaleDown", "detail": {...}}
            - Direct format: {"action": "...", ...} for manual operations
        context: Lambda context

    Returns:
        Response with operation status and details
    """
    # Health check endpoint
    if event.get("action") == "health_check":
        return _handle_health_check()

    logger.info("Worker Cleanup Lambda invoked")
    logger.info(f"Event: {json.dumps(event)}")

    try:
        # Handle EventBridge events
        detail_type = event.get("detail-type")

        if detail_type == "ScaleDown":
            # EventBridge event from main autoscaler
            detail = event.get("detail", {})
            logger.info(f"Scale-down event: {detail.get('reason')}")
            return _handle_scale_down(detail)
        elif detail_type:
            # Unknown EventBridge event
            return {
                "statusCode": 400,
                "body": json.dumps({"error": f"Unknown event type: {detail_type}"}),
            }

        # Handle direct invocation (for manual operations)
        action = event.get("action", "list")

        if action == "list":
            return _list_workers()
        elif action == "describe":
            instance_id = event.get("instance_id")
            if not instance_id:
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "instance_id required for describe action"}),
                }
            return _describe_worker(instance_id)
        elif action == "drain":
            instance_id = event.get("instance_id")
            if not instance_id:
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "instance_id required for drain action"}),
                }
            return _drain_worker(instance_id)
        elif action == "terminate":
            instance_id = event.get("instance_id")
            if not instance_id:
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "instance_id required for terminate action"}),
                }
            return _terminate_worker(instance_id)
        elif action == "scale_down":
            return _scale_down()
        elif action == "clean_stale_nodes":
            return _clean_stale_nodes()
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


def _get_cluster_name() -> str:
    """Get cluster name from environment."""
    cluster_name = os.environ.get("CLUSTER_NAME", "production-k3s")
    return cluster_name


def _get_state_table_name() -> str:
    """Get DynamoDB state table name from environment."""
    return os.environ.get("STATE_TABLE_NAME")


def _acquire_distributed_lock(table_name: str, cluster_name: str, timeout_seconds: int = 10) -> tuple[bool, str]:
    """Acquire distributed lock for scaling operation.

    Args:
        table_name: DynamoDB table name
        cluster_name: Cluster identifier
        timeout_seconds: Lock acquisition timeout

    Returns:
        Tuple of (acquired: bool, lock_id: str)
    """
    dynamodb = boto3.client("dynamodb")

    # Current time and lock expiry
    now = datetime.now(timezone.utc)
    lock_id = f"scale-down-{now.isoformat()}"
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
        logger.info("✓ Acquired distributed lock for scale-down")
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


def _handle_scale_down(detail: dict) -> dict:
    """Handle scale-down event from EventBridge.

    Executes scale-down operation and updates DynamoDB state.

    Args:
        detail: Event detail containing scaling decision

    Returns:
        Response with operation status
    """
    current_nodes = detail.get("current_nodes", 0)
    target_nodes = detail.get("target_nodes", current_nodes - 1)
    reason = detail.get("reason", "")

    logger.info(f"Scale-down: {current_nodes} -> {target_nodes} nodes ({reason})")

    # Step 1: Acquire distributed lock
    table_name = _get_state_table_name()
    cluster_name = _get_cluster_name()
    lock_id = ""

    if table_name:
        lock_acquired, lock_id = _acquire_distributed_lock(table_name, cluster_name)
        if not lock_acquired:
            logger.warning("Could not acquire distributed lock - another scaling operation in progress")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "skipped",
                    "reason": "Distributed lock not available - another scaling operation in progress",
                }),
            }

    # Step 2: Execute scale-down (drain + terminate)
    try:
        scale_result = _scale_down()

        if scale_result["statusCode"] == 200:
            # Check if scale-down was skipped (no eligible workers)
            result_body = json.loads(scale_result.get("body", "{}"))
            if result_body.get("status") == "skipped":
                logger.info("Scale-down skipped (no eligible workers)")
                return scale_result

            # Update DynamoDB state with new node count
            if table_name:
                try:
                    dynamodb = boto3.client("dynamodb")
                    new_node_count = current_nodes - 1

                    dynamodb.update_item(
                        TableName=table_name,
                        Key={"cluster_id": {"S": cluster_name}},
                        UpdateExpression=(
                            "SET node_count = :node_count, "
                            "scaling_in_progress = :false, "
                            "last_scale_operation = :op, "
                            "last_scale_time = :time"
                        ),
                        ExpressionAttributeValues={
                            ":node_count": {"N": str(new_node_count)},
                            ":false": {"S": "false"},
                            ":op": {"S": "SCALE_DOWN"},
                            ":time": {"S": datetime.now(timezone.utc).isoformat()},
                        },
                    )

                    logger.info(f"✓ State updated: node_count={new_node_count}")

                except ClientError as e:
                    logger.error(f"Failed to update DynamoDB state: {e}")
                    # Don't fail the operation - scale-down succeeded

            # Final summary logging
            logger.info("=" * 60)
            logger.info("✓ SCALE-DOWN WORKFLOW COMPLETED")
            logger.info(f"  Previous node count: {current_nodes}")
            logger.info(f"  New node count: {new_node_count}")
            logger.info(f"  Cluster: {cluster_name}")
            logger.info("=" * 60)

            return scale_result
        else:
            logger.error(f"Scale-down operation failed: {scale_result}")
            return scale_result
    finally:
        # Step 3: Always release the lock
        if table_name and lock_id:
            _release_distributed_lock(table_name, cluster_name, lock_id)


def _list_workers() -> dict:
    """List all worker instances in the cluster."""
    logger.info("Listing worker instances")

    ec2_client = boto3.client("ec2")
    cluster_name = _get_cluster_name()

    response = ec2_client.describe_instances(
        Filters=[
            {"Name": "tag:Cluster", "Values": [cluster_name]},
            {"Name": "tag:NodeRole", "Values": ["worker"]},
            {"Name": "instance-state-name", "Values": ["running", "pending"]},
        ]
    )

    workers = []
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            # Extract tags as dict
            tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}

            worker_info = {
                "instance_id": instance["InstanceId"],
                "state": instance["State"]["Name"],
                "private_ip": instance.get("PrivateIpAddress", "N/A"),
                "instance_type": instance["InstanceType"],
                "launch_time": instance.get("LaunchTime"),
                "tags": tags,
                "is_permanent": tags.get("Permanent", "false").lower() == "true",
                "created_by": tags.get("CreatedBy", "unknown"),
            }
            workers.append(worker_info)

    # Sort: permanent first, then by launch time (oldest first for scale down selection)
    workers.sort(key=lambda w: (not w["is_permanent"], w["launch_time"] or ""))

    logger.info(f"Found {len(workers)} worker instances")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "cluster": cluster_name,
            "worker_count": len(workers),
            "workers": workers,
        }, indent=2, default=str),
    }


def _describe_worker(instance_id: str) -> dict:
    """Describe a specific worker instance."""
    logger.info(f"Describing worker: {instance_id}")

    ec2_client = boto3.client("ec2")

    response = ec2_client.describe_instances(InstanceIds=[instance_id])
    instance = response["Reservations"][0]["Instances"][0]

    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}

    worker_info = {
        "instance_id": instance["InstanceId"],
        "state": instance["State"],
        "private_ip": instance.get("PrivateIpAddress", "N/A"),
        "public_ip": instance.get("PublicIpAddress", "N/A"),
        "instance_type": instance["InstanceType"],
        "launch_time": instance.get("LaunchTime"),
        "tags": tags,
        "is_permanent": tags.get("Permanent", "false").lower() == "true",
    }

    return {
        "statusCode": 200,
        "body": json.dumps(worker_info, indent=2, default=str),
    }


def _drain_worker(instance_id: str) -> dict:
    """Drain a worker node using kubectl via SSM.

    This executes kubectl drain on the master node via SSM,
    which evicts pods and marks the node as unschedulable.
    """
    logger.info(f"Draining worker node: {instance_id}")

    # Get master IP from SSM and look up master instance ID
    cluster_name = _get_cluster_name()
    ec2_client = boto3.client("ec2")
    ssm_client = boto3.client("ssm")

    try:
        master_ip_response = ssm_client.get_parameter(
            Name=f"/k3s/{cluster_name}/master-ip"
        )
        master_ip = master_ip_response["Parameter"]["Value"]
        logger.info(f"Master IP: {master_ip}")
    except ClientError as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": f"Failed to get master IP from SSM: {e}",
            }),
        }

    # Look up master instance ID from master IP
    try:
        master_response = ec2_client.describe_instances(
            Filters=[{"Name": "private-ip-address", "Values": [master_ip]}]
        )
        master_instance_id = master_response["Reservations"][0]["Instances"][0]["InstanceId"]
        logger.info(f"Master instance ID: {master_instance_id}")
    except (ClientError, IndexError, KeyError) as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": f"Failed to find master instance for IP {master_ip}: {e}",
            }),
        }
    instance_response = ec2_client.describe_instances(InstanceIds=[instance_id])
    private_ip = instance_response["Reservations"][0]["Instances"][0].get("PrivateIpAddress")

    if not private_ip:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": f"Could not determine private IP for instance {instance_id}",
            }),
        }

    # K3s node names use dots, not dashes: ip-10.0.2.42 not ip-10-0-2-42
    node_name = f"ip-{private_ip}"
    logger.info(f"Node name: {node_name}")

    # Execute kubectl drain and kubectl delete via SSM on master
    drain_command = f"sudo kubectl drain {node_name} --ignore-daemonsets --delete-emptydir-data --timeout=120s"
    delete_command = f"sudo kubectl delete node {node_name} --ignore-not-found=true"

    logger.info("Executing drain command via SSM on master")
    try:
        ssm_response = ssm_client.send_command(
            InstanceIds=[master_instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [drain_command, delete_command]},
            TimeoutSeconds=120,
        )

        command_id = ssm_response["Command"]["CommandId"]
        logger.info(f"SSM Command ID: {command_id}")

        # Wait for command to complete
        import time
        time.sleep(5)  # Initial wait

        max_attempts = 24  # 2 minutes (5 second intervals)
        for attempt in range(max_attempts):
            try:
                result = ssm_client.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=master_instance_id,
                )

                status = result["Status"]
                logger.info(f"Poll attempt {attempt + 1}/{max_attempts}: status={status}")

                if status in ["Success", "Failed", "TimedOut", "Cancelled"]:
                    break

                time.sleep(5)
            except ClientError as e:
                logger.warning(f"Poll attempt {attempt + 1}/{max_attempts}: SSM error - {e}")
                time.sleep(5)

        # Get final result
        try:
            result = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=master_instance_id,
            )

            stdout = result.get("StandardOutputContent", "")
            stderr = result.get("StandardErrorContent", "")
            status = result["Status"]

            logger.info(f"Drain command status: {status}")

            if status == "Success":
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "message": f"Node {node_name} drained successfully",
                        "instance_id": instance_id,
                        "node_name": node_name,
                        "status": status,
                        "output": stdout[-500:] if len(stdout) > 500 else stdout,
                    }),
                }
            else:
                return {
                    "statusCode": 500,
                    "body": json.dumps({
                        "message": f"Drain failed with status: {status}",
                        "instance_id": instance_id,
                        "node_name": node_name,
                        "status": status,
                        "stderr": stderr[-500:] if len(stderr) > 500 else stderr,
                    }),
                }
        except ClientError as e:
            return {
                "statusCode": 500,
                "body": json.dumps({
                    "error": f"Failed to get command result: {e}",
                }),
            }

    except ClientError as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": f"Failed to send SSM command: {e}",
            }),
        }


def _uninstall_k3s_worker(instance_id: str) -> dict:
    """Uninstall K3s agent from worker before termination.

    This runs k3s-agent-uninstall.sh on the worker to ensure clean
    removal from the Kubernetes cluster.
    """
    logger.info(f"Uninstalling K3s agent from: {instance_id}")

    ssm_client = boto3.client("ssm")

    uninstall_command = "sudo /usr/local/bin/k3s-agent-uninstall.sh"

    logger.info("Executing uninstall script via SSM on worker")
    try:
        ssm_response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [uninstall_command]},
            TimeoutSeconds=60,
        )

        command_id = ssm_response["Command"]["CommandId"]
        logger.info(f"SSM Command ID: {command_id}")

        # Wait for command to complete
        import time
        time.sleep(5)  # Initial wait

        max_attempts = 12  # 1 minute (5 second intervals)
        for attempt in range(max_attempts):
            try:
                result = ssm_client.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=instance_id,
                )

                status = result["Status"]
                logger.info(f"Uninstall attempt {attempt + 1}/{max_attempts}: status={status}")

                if status in ["Success", "Failed", "TimedOut", "Cancelled"]:
                    break

                time.sleep(5)
            except ClientError as e:
                logger.warning(f"Uninstall attempt {attempt + 1}/{max_attempts}: SSM error - {e}")
                time.sleep(5)

        # Get final result
        try:
            result = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )

            stdout = result.get("StandardOutputContent", "")
            stderr = result.get("StandardErrorContent", "")
            status = result["Status"]

            logger.info(f"Uninstall command status: {status}")

            if status == "Success":
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "message": "K3s agent uninstalled successfully",
                        "instance_id": instance_id,
                        "status": status,
                        "output": stdout[-500:] if len(stdout) > 500 else stdout,
                    }),
                }
            else:
                # Log warning but don't fail - instance will still be terminated
                logger.warning(f"Uninstall failed with status: {status}, stderr: {stderr[-200:]}")
                return {
                    "statusCode": 200,  # Don't fail termination
                    "body": json.dumps({
                        "message": f"Uninstall completed with status: {status}",
                        "instance_id": instance_id,
                        "status": status,
                        "note": "Proceeding with termination",
                    }),
                }
        except ClientError as e:
            logger.warning(f"Failed to get uninstall result: {e}, proceeding with termination")
            return {
                "statusCode": 200,  # Don't fail termination
                "body": json.dumps({
                    "message": "Could not verify uninstall, proceeding with termination",
                    "instance_id": instance_id,
                }),
            }

    except ClientError as e:
        logger.warning(f"Failed to send uninstall command: {e}, proceeding with termination")
        return {
            "statusCode": 200,  # Don't fail termination
            "body": json.dumps({
                "message": "Could not run uninstall, proceeding with termination",
                "instance_id": instance_id,
            }),
        }


def _terminate_worker(instance_id: str) -> dict:
    """Terminate a worker instance."""
    logger.info(f"Terminating worker: {instance_id}")

    ec2_client = boto3.client("ec2")

    try:
        # First, uninstall K3s agent for clean cluster removal
        uninstall_result = _uninstall_k3s_worker(instance_id)
        uninstall_data = json.loads(uninstall_result["body"])
        logger.info(f"Uninstall result: {uninstall_data.get('message')}")

        # Then terminate the instance
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


def _scale_down() -> dict:
    """Scale down by selecting and terminating a non-permanent worker.

    Uses LIFO (Last In, First Out) strategy:
    - Excludes instances with Permanent=true tag
    - Prefers instances with CreatedBy=autoscaler
    - Selects the most recently launched
    """
    logger.info("Executing SCALE_DOWN operation")

    ec2_client = boto3.client("ec2")
    cluster_name = _get_cluster_name()

    # Get all workers
    response = ec2_client.describe_instances(
        Filters=[
            {"Name": "tag:Cluster", "Values": [cluster_name]},
            {"Name": "tag:NodeRole", "Values": ["worker"]},
            {"Name": "instance-state-name", "Values": ["running", "pending"]},
        ]
    )

    workers = []
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}

            # Skip permanent workers
            if tags.get("Permanent", "false").lower() == "true":
                logger.info(f"Skipping permanent worker: {instance['InstanceId']}")
                continue

            workers.append({
                "instance": instance,
                "tags": tags,
                "launch_time": instance.get("LaunchTime", ""),
            })

    if not workers:
        logger.info("No eligible workers found for scale-down (all workers are permanent)")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "skipped",
                "message": "No eligible instance found for scale-down (all workers are permanent)",
            }),
        }

    # Sort by launch time (most recent first) for LIFO
    workers.sort(key=lambda w: w["launch_time"] or "", reverse=True)

    # Prefer autoscaler-created instances
    autoscaler_workers = [
        w for w in workers
        if w["tags"].get("CreatedBy") == "autoscaler"
    ]

    selected = autoscaler_workers[0] if autoscaler_workers else workers[0]
    instance_id = selected["instance"]["InstanceId"]

    logger.info(f"Selected worker for scale-down: {instance_id}")

    # First drain the node
    drain_result = _drain_worker(instance_id)
    drain_data = json.loads(drain_result["body"])

    if drain_result["statusCode"] == 200:
        logger.info("Node drained successfully, proceeding with termination")

        # Terminate instance
        terminate_result = _terminate_worker(instance_id)
        terminate_data = json.loads(terminate_result["body"])

        # Comprehensive success logging
        logger.info("=" * 60)
        logger.info("✓ SCALE-DOWN OPERATION COMPLETED SUCCESSFULLY")
        logger.info(f"  Instance ID: {instance_id}")
        logger.info(f"  Node Name: {drain_data.get('node_name', 'N/A')}")
        logger.info("  Node Drained: Yes")
        logger.info("  Node Deleted: Yes")
        logger.info("  Instance Terminated: Yes")
        logger.info(f"  Drain Status: {drain_data.get('status', 'N/A')}")
        logger.info(f"  Termination Status: {terminate_data.get('status', 'N/A')}")
        logger.info("=" * 60)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Scale-down completed",
                "instance_id": instance_id,
                "node_name": drain_data.get("node_name", "N/A"),
                "drain_result": drain_data,
                "terminate_result": terminate_data,
            }),
        }
    else:
        logger.warning(f"Drain failed, aborting termination: {drain_data}")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Scale-down aborted due to drain failure",
                "instance_id": instance_id,
                "drain_error": drain_data,
            }),
        }


def _clean_stale_nodes() -> dict:
    """Remove NotReady nodes from the Kubernetes cluster using SSM.

    This identifies nodes that are NotReady (terminated instances but not
    removed from Kubernetes) and deletes them one-by-one via kubectl on the master.
    Each deletion uses a separate SSM command for atomicity and better logging.
    """
    logger.info("Executing CLEAN_STALE_NODES operation")

    cluster_name = _get_cluster_name()
    ssm_client = boto3.client("ssm")
    ec2_client = boto3.client("ec2")

    # Step 1: Get master IP and instance ID
    try:
        master_ip_response = ssm_client.get_parameter(
            Name=f"/k3s/{cluster_name}/master-ip"
        )
        master_ip = master_ip_response["Parameter"]["Value"]
        logger.info(f"Master IP: {master_ip}")
    except ClientError as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": f"Failed to get master IP from SSM: {e}",
            }),
        }

    try:
        master_response = ec2_client.describe_instances(
            Filters=[{"Name": "private-ip-address", "Values": [master_ip]}]
        )
        master_instance_id = master_response["Reservations"][0]["Instances"][0]["InstanceId"]
        logger.info(f"Master instance ID: {master_instance_id}")
    except (ClientError, IndexError, KeyError) as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": f"Failed to find master instance for IP {master_ip}: {e}",
            }),
        }

    # Step 2: Get list of NotReady nodes via SSM
    find_stale_command = "sudo kubectl get nodes --no-headers -o custom-columns=NAME:.metadata.name,ROLE:.metadata.labels.kubernetes\\.io/role,STATUS:.status.phase | awk '$3 == \"NotReady\" {print $1}'"

    logger.info("Finding stale nodes via SSM on master")
    try:
        ssm_response = ssm_client.send_command(
            InstanceIds=[master_instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [find_stale_command]},
            TimeoutSeconds=30,
        )

        command_id = ssm_response["Command"]["CommandId"]

        # Wait for command to complete
        import time
        time.sleep(5)

        max_attempts = 6  # 30 seconds
        for attempt in range(max_attempts):
            result = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=master_instance_id,
            )
            if result["Status"] in ["Success", "Failed", "TimedOut", "Cancelled"]:
                break
            time.sleep(5)

        stdout = result.get("StandardOutputContent", "")
        stderr = result.get("StandardErrorContent", "")

        if result["Status"] != "Success":
            return {
                "statusCode": 500,
                "body": json.dumps({
                    "error": f"Failed to find stale nodes: {stderr}",
                }),
            }

        # Parse node names from output
        stale_nodes = [line.strip() for line in stdout.split('\n') if line.strip()]

        if not stale_nodes:
            logger.info("No stale nodes found")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "No stale nodes found",
                    "nodes_deleted": 0,
                }),
            }

        logger.info(f"Found {len(stale_nodes)} stale nodes: {stale_nodes}")

    except ClientError as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": f"Failed to query stale nodes: {e}",
            }),
        }

    # Step 3: Delete each stale node individually via SSM
    nodes_deleted = 0
    failed_nodes = []

    for node_name in stale_nodes:
        logger.info(f"Deleting stale node: {node_name}")

        delete_command = f"sudo kubectl delete node {node_name} --ignore-not-found=true"

        try:
            ssm_response = ssm_client.send_command(
                InstanceIds=[master_instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [delete_command]},
                TimeoutSeconds=30,
            )

            command_id = ssm_response["Command"]["CommandId"]

            # Wait for command to complete
            time.sleep(3)

            max_attempts = 6  # 30 seconds
            for attempt in range(max_attempts):
                result = ssm_client.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=master_instance_id,
                )
                if result["Status"] in ["Success", "Failed", "TimedOut", "Cancelled"]:
                    break
                time.sleep(5)

            stdout = result.get("StandardOutputContent", "")
            stderr = result.get("StandardErrorContent", "")

            if result["Status"] == "Success":
                nodes_deleted += 1
                logger.info(f"✓ Deleted stale node: {node_name}")
            else:
                failed_nodes.append(node_name)
                logger.warning(f"✗ Failed to delete node {node_name}: {stderr}")

        except ClientError as e:
            failed_nodes.append(node_name)
            logger.warning(f"✗ Failed to send delete command for {node_name}: {e}")

    # Return summary
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": f"Stale node cleanup completed: {nodes_deleted} deleted, {len(failed_nodes)} failed",
            "nodes_deleted": nodes_deleted,
            "failed_nodes": failed_nodes,
        }),
    }
