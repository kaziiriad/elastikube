"""Failed Instance Cleanup Lambda handler.

This Lambda runs periodically (every 5 minutes) to detect and terminate instances that:
1. Were launched by the autoscaler (CreatedBy=autoscaler tag)
2. Have been running for > 5 minutes
3. Never joined the cluster (no JoinVerified=true tag)
4. OR are explicitly marked for cleanup (CleanupRequired=true tag)

This prevents orphaned instances from continuing to run and incur costs.

Triggered by: EventBridge rule (rate(5 minutes))

Environment variables required:
    CLUSTER_NAME: Name of the K3s cluster (default: production-k3s)
    MAX_INSTANCE_AGE_MINUTES: Maximum age before considering failed (default: 5)
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: dict, context: Any) -> dict:
    """Lambda entry point for cleanup operations.

    Args:
        event: Lambda event
            - {"action": "health_check"} for health check
            - EventBridge trigger for periodic cleanup
        context: Lambda context

    Returns:
        Response with operation status
    """
    # Health check endpoint
    if event.get("action") == "health_check":
        return _handle_health_check()

    logger.info("Cleanup Lambda invoked")

    cluster_name = os.environ.get("CLUSTER_NAME", "production-k3s")
    max_age_minutes = int(os.environ.get("MAX_INSTANCE_AGE_MINUTES", "5"))

    try:
        # Find failed instances
        failed_instances = _find_failed_instances(cluster_name, max_age_minutes)

        if not failed_instances:
            logger.info("No failed instances found")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "No cleanup needed",
                    "terminated_count": 0,
                }),
            }

        logger.info(f"Found {len(failed_instances)} failed instances to clean up")

        # Terminate failed instances
        terminated = []
        for instance in failed_instances:
            instance_id = instance["InstanceId"]
            reason = _get_failure_reason(instance)

            logger.info(f"Terminating {instance_id}: {reason}")

            success = _terminate_instance(instance_id)
            if success:
                terminated.append({
                    "instance_id": instance_id,
                    "reason": reason,
                    "launch_time": instance.get("LaunchTime"),
                })

        logger.info(f"✓ Cleaned up {len(terminated)} failed instances")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": f"Cleaned up {len(terminated)} failed instances",
                "terminated_count": len(terminated),
                "terminated_instances": terminated,
            }, default=str),
        }

    except Exception as e:
        logger.exception("Cleanup failed")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": str(e),
            }),
        }


def _handle_health_check() -> dict:
    """Handle health check requests.

    Returns the health status of the cleanup Lambda.

    Returns:
        Health check response with component statuses
    """
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lambda": "cleanup-lambda",
        "components": {},
    }

    try:
        ec2_client = boto3.client("ec2")

        # Check EC2 connectivity
        try:
            ec2_client.describe_instances(MaxResults=1)
            health_status["components"]["ec2"] = {"status": "healthy"}
        except Exception as e:
            health_status["components"]["ec2"] = {"status": "unhealthy", "error": str(e)}
            health_status["status"] = "degraded"

        # Check configuration
        cluster_name = os.environ.get("CLUSTER_NAME")
        max_age = os.environ.get("MAX_INSTANCE_AGE_MINUTES", "5")

        health_status["components"]["config"] = {
            "status": "healthy" if cluster_name else "degraded",
            "cluster_name": cluster_name,
            "max_age_minutes": int(max_age) if max_age else 5,
        }

    except Exception as e:
        health_status["status"] = "unhealthy"
        health_status["error"] = str(e)

    return {
        "statusCode": 200 if health_status["status"] in ("healthy", "degraded") else 503,
        "body": json.dumps(health_status, indent=2),
    }


def _find_failed_instances(cluster_name: str, max_age_minutes: int) -> list[dict]:
    """Find instances that failed to join cluster.

    Criteria:
    1. Created by autoscaler (CreatedBy=autoscaler tag)
    2. Running for > max_age_minutes
    3. No JoinVerified=true tag OR CleanupRequired=true tag

    Args:
        cluster_name: Cluster name
        max_age_minutes: Maximum age before considering failed

    Returns:
        List of failed instance dictionaries
    """
    ec2_client = boto3.client("ec2")

    try:
        response = ec2_client.describe_instances(
            Filters=[
                {"Name": "tag:Cluster", "Values": [cluster_name]},
                {"Name": "tag:NodeRole", "Values": ["worker"]},
                {"Name": "tag:CreatedBy", "Values": ["autoscaler"]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        )

        failed = []
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)

        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}

                # Check if marked for cleanup
                if tags.get("CleanupRequired") == "true":
                    logger.info(f"Instance {instance['InstanceId']} marked for cleanup")
                    failed.append(instance)
                    continue

                # Check if old and not verified
                launch_time = instance.get("LaunchTime")
                if launch_time:
                    # Ensure timezone-aware
                    if launch_time.tzinfo is None:
                        launch_time = launch_time.replace(tzinfo=timezone.utc)

                    if launch_time < cutoff_time:
                        # Instance is old enough
                        if tags.get("JoinVerified") != "true":
                            instance_id = instance['InstanceId']
                            private_ip = instance.get('PrivateIpAddress', '')

                            # Verify if node actually joined cluster before terminating
                            if private_ip:
                                check_result = _check_node_in_cluster(instance_id, private_ip, cluster_name)

                                if check_result["in_cluster"]:
                                    # Node IS in cluster - tag it as verified and skip termination
                                    logger.info(f"Instance {instance_id} is in cluster as {check_result['node_name']} "
                                               f"(status: {check_result['status']}) - tagging as verified")

                                    try:
                                        ec2_client.create_tags(
                                            Resources=[instance_id],
                                            Tags=[
                                                {"Key": "JoinVerified", "Value": "true"},
                                                {"Key": "VerifiedAt", "Value": datetime.now(timezone.utc).isoformat()},
                                                {"Key": "VerificationMethod", "Value": "kubectl_fallback"},
                                            ]
                                        )
                                    except Exception as e:
                                        logger.warning(f"Failed to tag instance: {e}")
                                    continue
                                else:
                                    # Node NOT in cluster - safe to terminate
                                    age_seconds = int((datetime.now(timezone.utc) - launch_time).total_seconds())
                                    logger.info(f"Instance {instance_id} NOT in cluster (check: {check_result['status']}), "
                                               f"age: {age_seconds}s - marking for termination")
                                    failed.append(instance)
                            else:
                                # No private IP - can't verify, terminate to be safe
                                age_seconds = int((datetime.now(timezone.utc) - launch_time).total_seconds())
                                logger.warning(f"Instance {instance_id} has no private IP, marking for termination (age: {age_seconds}s)")
                                failed.append(instance)

        return failed

    except ClientError as e:
        logger.error(f"Failed to describe instances: {e}")
        return []


def _get_failure_reason(instance: dict) -> str:
    """Get failure reason from instance tags.

    Args:
        instance: EC2 instance dict

    Returns:
        Failure reason string
    """
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}

    if tags.get("CleanupRequired") == "true":
        return tags.get("CleanupReason", "Marked for cleanup")

    launch_time = instance.get("LaunchTime")
    if launch_time:
        if launch_time.tzinfo is None:
            launch_time = launch_time.replace(tzinfo=timezone.utc)
        age_seconds = int((datetime.now(timezone.utc) - launch_time).total_seconds())
        age_minutes = age_seconds // 60
        return f"Failed to join cluster after {age_minutes} minutes"

    return "Unknown failure"


def _check_node_in_cluster(instance_id: str, private_ip: str, cluster_name: str) -> dict:
    """Check if node actually joined the cluster via kubectl on master.

    Uses S3 script to verify cluster membership before terminating instances.

    Args:
        instance_id: EC2 instance ID
        private_ip: Instance private IP address
        cluster_name: Cluster name for SSM parameter lookup

    Returns:
        Dict with in_cluster (bool), node_name, status
    """
    import time

    ssm_client = boto3.client("ssm")
    aws_region = os.environ.get("AWS_REGION", "ap-southeast-1")

    try:
        # Get master IP from SSM
        master_ip_response = ssm_client.get_parameter(
            Name=f"/k3s/{cluster_name}/master-ip"
        )
        master_ip = master_ip_response["Parameter"]["Value"]

        # Get master instance ID
        ec2_client = boto3.client("ec2")
        master_response = ec2_client.describe_instances(
            Filters=[{"Name": "private-ip-address", "Values": [master_ip]}]
        )
        master_instance_id = master_response["Reservations"][0]["Instances"][0]["InstanceId"]

        # Run the locally installed check-node-by-ip script on master
        check_command = f"""
            /usr/local/bin/check-node-by-ip.sh "{private_ip}"
        """

        result = ssm_client.send_command(
            InstanceIds=[master_instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [check_command]},
            TimeoutSeconds=30,
        )

        command_id = result["Command"]["CommandId"]
        time.sleep(8)  # Wait for command to complete

        # Get result
        invocation = ssm_client.get_command_invocation(
            CommandId=command_id,
            InstanceId=master_instance_id,
        )

        if invocation["Status"] == "Success":
            stdout = invocation.get("StandardOutputContent", "")

            # Parse output for NODE_NAME and NODE_READY
            result_dict = {}
            for line in stdout.split("\n"):
                if "=" in line:
                    key, value = line.split("=", 1)
                    result_dict[key.strip()] = value.strip()

            node_name = result_dict.get("NODE_NAME", "")
            node_ready = result_dict.get("NODE_READY", "False")
            node_status = result_dict.get("NODE_STATUS", "not_found")

            if node_name:
                # Node found in cluster!
                return {
                    "in_cluster": True,
                    "node_name": node_name,
                    "status": "ready" if node_ready == "True" else "not_ready",
                }
            else:
                return {
                    "in_cluster": False,
                    "node_name": None,
                    "status": "not_found",
                }
        else:
            # SSM command failed - assume not in cluster to be safe
            logger.warning(f"SSM check failed for {instance_id}: {invocation.get('StatusDetails', 'unknown')}")
            return {
                "in_cluster": False,
                "node_name": None,
                "status": "ssm_failed",
            }

    except Exception as e:
        logger.warning(f"Failed to check cluster membership for {instance_id}: {e}")
        # On error, assume not in cluster to be safe
        return {
            "in_cluster": False,
            "node_name": None,
            "status": "error",
        }


def _terminate_instance(instance_id: str) -> bool:
    """Terminate an instance.

    Args:
        instance_id: EC2 instance ID

    Returns:
        True if successful
    """
    ec2_client = boto3.client("ec2")

    try:
        ec2_client.terminate_instances(InstanceIds=[instance_id])
        logger.info(f"✓ Terminated {instance_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to terminate {instance_id}: {e}")
        return False
