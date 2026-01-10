"""Kubectl operations via SSM to K3s master node.

This module allows the Lambda to execute kubectl commands on the K3s master
node using AWS Systems Manager (SSM) Run Command. This enables node draining
without requiring direct Kubernetes API access from Lambda.
"""

import logging
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from utils.config import get_config

logger = logging.getLogger(__name__)


class KubectlViaSSM:
    """Execute kubectl commands on K3s master via SSM."""

    def __init__(self, ec2_client=None, ssm_client=None):
        """Initialize the SSM kubectl client.

        Args:
            ec2_client: Optional boto3 EC2 client
            ssm_client: Optional boto3 SSM client
        """
        self._config = get_config()
        self._ec2_client = ec2_client or boto3.client("ec2")
        self._ssm_client = ssm_client or boto3.client("ssm")

    def _get_master_instance_id(self) -> Optional[str]:
        """Get the K3s master instance ID from EC2.

        Returns:
            Master instance ID or None if not found
        """
        try:
            response = self._ec2_client.describe_instances(
                Filters=[
                    {"Name": "tag:Name", "Values": ["k3s-master"]},
                    {"Name": "instance-state-name", "Values": ["running"]},
                ]
            )

            reservations = response.get("Reservations", [])
            if not reservations or not reservations[0].get("Instances"):
                logger.error("No running k3s-master instance found")
                return None

            return reservations[0]["Instances"][0]["InstanceId"]

        except ClientError as e:
            logger.error(f"Failed to describe instances: {e}")
            return None

    def _wait_for_command(
        self,
        command_id: str,
        instance_id: str,
        timeout: int = 300,
    ) -> dict:
        """Wait for SSM command to complete and return output.

        Args:
            command_id: SSM command ID
            instance_id: EC2 instance ID
            timeout: Maximum time to wait in seconds

        Returns:
            Dict with status, stdout, stderr, exit_code
        """
        start_time = time.time()
        poll_interval = 5  # Check every 5 seconds

        while time.time() - start_time < timeout:
            try:
                output = self._ssm_client.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=instance_id,
                )

                status = output.get("Status")

                if status in ["Success", "Failed", "TimedOut", "Cancelled"]:
                    return {
                        "status": status,
                        "stdout": output.get("StandardOutputContent", ""),
                        "stderr": output.get("StandardErrorContent", ""),
                        "exit_code": output.get("ResponseCode", -1),
                    }

            except ClientError as e:
                logger.error(f"Failed to get command invocation: {e}")
                return {"status": "Failed", "error": str(e)}

            time.sleep(poll_interval)

        return {"status": "TimedOut", "error": f"Command did not complete within {timeout}s"}

    def drain_node(
        self,
        node_name: str,
        timeout: int = 120,
        delete_node: bool = True,
    ) -> dict:
        """Drain a Kubernetes node via SSM to master.

        This executes `kubectl drain` on the K3s master node, which:
        1. Marks the node as unschedulable (cordon)
        2. Evicts all pods (except DaemonSets and mirrored pods)
        3. Optionally deletes the node object from Kubernetes

        Args:
            node_name: Name of the node to drain (e.g., "ip-172-31-0-123")
            timeout: Timeout for kubectl drain command (default: 120s)
            delete_node: If True, delete node object after draining

        Returns:
            Dict with status and command output
        """
        master_instance_id = self._get_master_instance_id()
        if not master_instance_id:
            return {
                "status": "Failed",
                "error": "Could not find k3s-master instance"
            }

        logger.info(f"Draining node {node_name} via SSM to master {master_instance_id}")

        # Build kubectl drain command
        # --delete-emptydir-data: Remove pods with emptyDir volumes
        # --ignore-daemonsets: Skip DaemonSet pods (they run on all nodes)
        # --force: Force removal even if pods aren't gracefully terminating
        # --disable-eviction: Use delete instead of eviction (faster)
        drain_cmd = (
            f"sudo kubectl drain {node_name} "
            f"--delete-emptydir-data "
            f"--ignore-daemonsets "
            f"--force "
            f"--timeout={timeout}s"
        )

        # Build command list
        commands = [
            "#!/bin/bash",
            "set -e",
            "",
            f"echo 'Draining node: {node_name}'",
            drain_cmd,
        ]

        if delete_node:
            commands.append(f"")
            commands.append(f"echo 'Deleting node: {node_name}'")
            commands.append(f"sudo kubectl delete node {node_name}")

        try:
            # Send command via SSM
            response = self._ssm_client.send_command(
                InstanceIds=[master_instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": commands},
                TimeoutSeconds=timeout + 60,  # Extra buffer for SSM overhead
            )

            command_id = response["Command"]["CommandId"]
            logger.info(f"SSM command sent: {command_id}")

            # Wait for command to complete
            result = self._wait_for_command(
                command_id=command_id,
                instance_id=master_instance_id,
                timeout=timeout + 60,
            )

            if result["status"] == "Success":
                logger.info(f"Successfully drained node {node_name}")
            else:
                logger.warning(f"Drain command status: {result['status']}")
                if result.get("stderr"):
                    logger.warning(f"Drain stderr: {result['stderr']}")

            return {
                "node_name": node_name,
                "command_id": command_id,
                **result
            }

        except ClientError as e:
            logger.error(f"Failed to send SSM command: {e}")
            return {
                "status": "Failed",
                "error": str(e),
                "node_name": node_name,
            }

    def get_node_name_from_instance_id(self, instance_id: str) -> Optional[str]:
        """Get Kubernetes node name from EC2 instance ID.

        Args:
            instance_id: EC2 instance ID

        Returns:
            Kubernetes node name or None if not found
        """
        try:
            response = self._ec2_client.describe_instances(InstanceIds=[instance_id])
            instance = response["Reservations"][0]["Instances"][0]

            # Node name is typically the private IP with dashes
            # e.g., 172.31.0.123 -> ip-172-31-0-123
            private_ip = instance.get("PrivateIpAddress")
            if private_ip:
                node_name = private_ip.replace(".", "-")
                logger.info(f"Mapped instance {instance_id} to node {node_name}")
                return node_name

            # Fallback: check for Name tag
            for tag in instance.get("Tags", []):
                if tag["Key"] == "Name":
                    return tag["Value"]

            return None

        except ClientError as e:
            logger.error(f"Failed to describe instance {instance_id}: {e}")
            return None
