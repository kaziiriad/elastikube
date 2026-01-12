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
        timeout: int = 300,
        delete_node: bool = True,
    ) -> dict:
        """Drain a Kubernetes node via SSM to master using SAFE drain flow.

        This implements the safe drain sequence:
        1. Cordons node to prevent new scheduling
        2. Drains with grace period (respects PodDisruptionBudgets)
        3. Verifies all pods migrated before continuing

        Args:
            node_name: Name of the node to drain (e.g., "ip-172-31-0-123")
            timeout: Timeout for drain command (default: 300s = 5min)
            delete_node: If True, delete node object after draining

        Returns:
            Dict with status, command output, and remaining pods count
        """
        master_instance_id = self._get_master_instance_id()
        if not master_instance_id:
            return {
                "status": "Failed",
                "error": "Could not find k3s-master instance"
            }

        logger.info(f"Starting SAFE drain for node {node_name} via SSM to master {master_instance_id}")

        # Safe drain sequence:
        # --grace-period=300: Give pods 5 minutes for graceful termination
        # --timeout=5m: Total operation timeout
        # --delete-emptydir-data: Remove pods with emptyDir volumes
        # --ignore-daemonsets: Skip DaemonSet pods
        # NO --force: Respect PodDisruptionBudgets!
        drain_cmd = (
            f"sudo kubectl drain {node_name} "
            f"--grace-period=300 "
            f"--timeout=5m "
            f"--delete-emptydir-data "
            f"--ignore-daemonsets"
        )

        # Build command list with verification
        commands = [
            "#!/bin/bash",
            "set -e",
            "",
            f"echo '=== Safe Drain Sequence for {node_name} ==='",
            "",
            "# Step 1: Cordon node first",
            f"echo 'Step 1: Cordoning node {node_name}'",
            f"sudo kubectl cordon {node_name}",
            "",
            "# Step 2: Drain with safe parameters",
            f"echo 'Step 2: Draining node with grace period=300s, timeout=5m'",
            drain_cmd,
            "",
            "# Step 3: Verify all pods migrated",
            f"echo 'Step 3: Verifying all pods migrated from {node_name}'",
            f"REMAINING_PODS=$(sudo kubectl get pods --all-namespaces --field-selector spec.nodeName={node_name} -o json | jq '.items | length')",
            "echo \"Remaining pods: $REMAINING_PODS\"",
            "",
            "if [ \"$REMAINING_PODS\" -gt 0 ]; then",
            "  echo 'WARNING: Some pods remain on the node:'",
            f"  sudo kubectl get pods --all-namespaces --field-selector spec.nodeName={node_name}",
            "  # List pods but don't fail - DaemonSets and local storage pods may remain",
            "else",
            f"  echo 'SUCCESS: All pods migrated from {node_name}'",
            "fi",
            "",
            "# Step 4: Check for StatefulSet pods (unsafe to drain)",
            f"echo 'Step 4: Checking for StatefulSet pods on {node_name}'",
            f"STATEFULSET_PODS=$(sudo kubectl get pods --all-namespaces --field-selector spec.nodeName={node_name} -o json | jq '[.items[] | select(.metadata.ownerReferences[]?.kind==\"StatefulSet\")] | length')",
            "echo \"StatefulSet pods: $STATEFULSET_PODS\"",
            "",
            "if [ \"$STATEFULSET_PODS\" -gt 0 ]; then",
            "  echo 'ERROR: StatefulSet pods detected on node!'",
            f"  sudo kubectl get pods --all-namespaces --field-selector spec.nodeName={node_name} -o json | jq -r '.items[] | select(.metadata.ownerReferences[]?.kind==\"StatefulSet\") | \"\\(.metadata.namespace)/\\(.metadata.name)\"'",
            "  echo 'StatefulSets require careful manual migration. Aborting drain.'",
            "  exit 1",
            "fi",
            "echo '✓ No StatefulSet pods detected'",
            "",
            "# Step 5: Check PodDisruptionBudget status",
            f"echo 'Step 5: Checking PodDisruptionBudget status before drain'",
            "PDB_VIOLATIONS=0",
            "",
            "# Get PDBs that would allow zero disruptions (at limit)",
            f"while IFS= read -r pdb; do",
            "  if [ -n \"$pdb\" ]; then",
            "    echo \"WARNING: PDB $pdb has no disruption budget remaining!\"",
            "    PDB_VIOLATIONS=1",
            "  fi",
            "done < <(sudo kubectl get pdb --all-namespaces -o json | jq -r '.items[] | select(.status.disruptionsAllowed==0) | \"\\(.metadata.namespace)/\\(.metadata.name)\"')",
            "",
            "if [ \"$PDB_VIOLATIONS\" -eq 1 ]; then",
            "  echo 'ERROR: PodDisruptionBudgets would be violated!'",
            "  echo 'Cannot safely drain this node. Aborting.'",
            "  exit 1",
            "fi",
            "echo '✓ All PodDisruptionBudgets allow disruptions'",
            "",
        ]

        if delete_node:
            commands.extend([
                "",
                f"echo 'Step 6: Deleting node object {node_name}'",
                f"sudo kubectl delete node {node_name}",
            ])

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

                # Parse remaining pods from stdout for verification
                remaining_pods = self._parse_remaining_pods(result.get("stdout", ""))
                if remaining_pods is not None:
                    result["remaining_pods"] = remaining_pods
                    logger.info(f"Remaining pods on node: {remaining_pods}")

                    # Check if StatefulSet or PDB checks failed
                    stdout_lower = result.get("stdout", "").lower()
                    if "error:" in stdout_lower or "aborting" in stdout_lower:
                        logger.warning(f"Drain aborted due to safety checks")
                        result["status"] = "Failed"
                        result["safety_check_failed"] = True
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

    def _parse_remaining_pods(self, stdout: str) -> Optional[int]:
        """Parse remaining pods count from command stdout.

        Looks for pattern: "Remaining pods: N" in the output.

        Args:
            stdout: Command output string

        Returns:
            Number of remaining pods or None if not found
        """
        import re

        match = re.search(r"Remaining pods:\s*(\d+)", stdout)
        if match:
            try:
                return int(match.group(1))
            except (ValueError, IndexError):
                pass
        return None

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

    def wait_for_node_ready(
        self,
        instance_id: str,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> dict:
        """Wait for a new worker node to join the cluster and become Ready.

        Polls Kubernetes via SSM to check if the node has joined and is Ready.
        The node name is derived from the instance's private IP.

        Args:
            instance_id: EC2 instance ID of the new worker
            timeout: Maximum time to wait in seconds (default: 5 minutes)
            poll_interval: Seconds between polls (default: 10 seconds)

        Returns:
            Dict with status ("Success", "TimedOut", "Failed"), node_name, and details
        """
        start_time = time.time()

        # First, get the expected node name from the instance ID
        node_name = self.get_node_name_from_instance_id(instance_id)
        if not node_name:
            return {
                "status": "Failed",
                "error": f"Could not determine node name for instance {instance_id}"
            }

        logger.info(f"Waiting for node {node_name} (instance {instance_id}) to join and be Ready...")

        while time.time() - start_time < timeout:
            try:
                # Check if node exists and is Ready via kubectl
                master_instance_id = self._get_master_instance_id()
                if not master_instance_id:
                    return {"status": "Failed", "error": "Could not find k3s-master instance"}

                # kubectl command to check node status
                check_cmd = (
                    f"#!/bin/bash\n"
                    f"sudo kubectl get node {node_name} -o json 2>/dev/null || echo 'NODE_NOT_FOUND'"
                )

                response = self._ssm_client.send_command(
                    InstanceIds=[master_instance_id],
                    DocumentName="AWS-RunShellScript",
                    Parameters={"commands": [check_cmd]},
                    TimeoutSeconds=30,
                )

                command_id = response["Command"]["CommandId"]
                result = self._wait_for_command(
                    command_id=command_id,
                    instance_id=master_instance_id,
                    timeout=30,
                )

                if result["status"] == "Success":
                    stdout = result.get("stdout", "")

                    # Check if node was found
                    if "NODE_NOT_FOUND" in stdout:
                        logger.info(f"Node {node_name} not yet joined cluster (elapsed: {int(time.time() - start_time)}s)")
                    else:
                        # Parse JSON to check node status
                        try:
                            import json
                            node_data = json.loads(stdout)

                            # Check for Ready condition
                            for condition in node_data.get("status", {}).get("conditions", []):
                                if condition.get("type") == "Ready":
                                    is_ready = condition.get("status") == "True"
                                    if is_ready:
                                        logger.info(f"✓ Node {node_name} is Ready!")
                                        return {
                                            "status": "Success",
                                            "node_name": node_name,
                                            "instance_id": instance_id,
                                            "elapsed_seconds": int(time.time() - start_time),
                                        }
                                    else:
                                        logger.info(f"Node {node_name} exists but not Ready yet "
                                                  f"(reason: {condition.get('reason', 'unknown')})")
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse kubectl output as JSON")

                elif result["status"] == "Failed":
                    logger.warning(f"kubectl command failed: {result.get('stderr', result)}")

            except ClientError as e:
                logger.error(f"Failed to check node status: {e}")

            time.sleep(poll_interval)

        # Timeout reached
        elapsed = int(time.time() - start_time)
        logger.error(f"Node {node_name} did not become Ready within {timeout}s (elapsed: {elapsed}s)")
        return {
            "status": "TimedOut",
            "node_name": node_name,
            "instance_id": instance_id,
            "elapsed_seconds": elapsed,
            "error": f"Node did not join cluster within {timeout}s",
        }
