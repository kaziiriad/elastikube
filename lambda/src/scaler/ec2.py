"""EC2 operations for scaling worker nodes.

Handles:
- Launching new worker instances
- Terminating instances
- Tagging for identification
- LIFO scaling (respecting permanent workers)
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from utils.config import get_config
from utils.wal import WalEntry, OperationType, OperationState


class EC2Operations:
    """Manages EC2 instances for K3s worker nodes."""

    def __init__(self, ec2_client=None):
        """Initialize EC2 operations.

        Args:
            ec2_client: Optional boto3 EC2 client
        """
        self._client = ec2_client or boto3.client("ec2")
        self._config = get_config()

    def launch_worker(
        self,
        subnet_id: str,
        security_group_id: str,
        iam_instance_profile: str,
        ami_id: str,
        instance_type: str,
        user_data: Optional[str] = None,
    ) -> str:
        """Launch a new K3s worker instance.

        Args:
            subnet_id: Subnet ID to launch in
            security_group_id: Security group ID
            iam_instance_profile: IAM instance profile name
            ami_id: AMI ID for the instance
            instance_type: EC2 instance type
            user_data: Optional cloud-init user-data script for K3s join

        Returns:
            Instance ID of the launched instance

        Raises:
            RuntimeError: If launch fails
        """
        tags = [
            {"Key": "Name", "Value": f"k3s-worker-{uuid.uuid4().hex[:6]}"},
            {"Key": "Project", "Value": "k3s-autoscaler"},
            {"Key": "Cluster", "Value": self._config.cluster_name},
            {"Key": "NodeRole", "Value": "worker"},
            {"Key": "CreatedBy", "Value": "autoscaler"},
            {"Key": "LaunchTime", "Value": datetime.now(timezone.utc).isoformat()},
        ]

        try:
            # Build run_instances parameters
            run_params = {
                "ImageId": ami_id,
                "InstanceType": instance_type,
                "MinCount": 1,
                "MaxCount": 1,
                "SubnetId": subnet_id,
                "SecurityGroupIds": [security_group_id],
                "IamInstanceProfile": {"Name": iam_instance_profile},
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": tags,
                    }
                ],
                "ClientToken": str(uuid.uuid4()),  # Idempotency token
            }

            # Add user-data if provided (must be base64-encoded)
            if user_data:
                import base64
                run_params["UserData"] = base64.b64encode(
                    user_data.encode("utf-8")
                ).decode("utf-8")

            # Use ClientToken for idempotency (prevents duplicate launches)
            response = self._client.run_instances(**run_params)

            instance_id = response["Instances"][0]["InstanceId"]
            return instance_id

        except ClientError as e:
            raise RuntimeError(f"Failed to launch EC2 instance: {e}") from e

    def terminate_instance(self, instance_id: str) -> None:
        """Terminate a worker instance.

        Args:
            instance_id: EC2 instance ID to terminate

        Raises:
            RuntimeError: If termination fails
        """
        try:
            self._client.terminate_instances(InstanceIds=[instance_id])
        except ClientError as e:
            raise RuntimeError(f"Failed to terminate instance {instance_id}: {e}") from e

    def get_worker_instances(self) -> list[dict]:
        """Get all worker instances in the cluster.

        Returns:
            List of worker instance details

        Raises:
            RuntimeError: If describe instances fails
        """
        try:
            response = self._client.describe_instances(
                Filters=[
                    {"Name": "tag:Cluster", "Values": [self._config.cluster_name]},
                    {"Name": "tag:NodeRole", "Values": ["worker"]},
                    {"Name": "instance-state-name", "Values": ["running", "pending"]},
                ]
            )

            instances = []
            for reservation in response["Reservations"]:
                instances.extend(reservation["Instances"])

            return instances

        except ClientError as e:
            raise RuntimeError(f"Failed to describe instances: {e}") from e

    def get_instance_for_scale_down(self) -> Optional[dict]:
        """Select a worker instance to terminate using LIFO strategy.

        LIFO (Last In, First Out):
        - Exclude instances with Permanent=true tag
        - Prefer instances with CreatedBy=autoscaler
        - Select the most recently launched

        Returns:
            Instance dict or None if no eligible instance
        """
        workers = self.get_worker_instances()

        # Filter out permanent workers
        candidates = [
            w for w in workers
            if not any(
                t.get("Key") == "Permanent" and t.get("Value") == "true"
                for t in w.get("Tags", [])
            )
        ]

        if not candidates:
            return None

        # Sort by launch time (most recent first) for LIFO
        candidates.sort(
            key=lambda i: i.get("LaunchTime", ""),
            reverse=True,
        )

        # Prefer autoscaler-created instances
        autoscaler_created = [
            i for i in candidates
            if any(
                t.get("Key") == "CreatedBy" and t.get("Value") == "autoscaler"
                for t in i.get("Tags", [])
            )
        ]

        if autoscaler_created:
            return autoscaler_created[0]

        return candidates[0]

    def wait_for_instance_ready(
        self,
        instance_id: str,
        timeout_seconds: int = 300,
        check_interval: int = 10,
    ) -> bool:
        """Wait for instance to be ready (running state).

        Note: This only checks EC2 state, not K3s node readiness.
        K3s node readiness should be verified separately via kubectl.

        Args:
            instance_id: EC2 instance ID
            timeout_seconds: Maximum time to wait
            check_interval: Seconds between checks

        Returns:
            True if instance is running, False if timeout
        """
        import time

        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            try:
                response = self._client.describe_instance_status(
                    InstanceIds=[instance_id],
                    IncludeAllInstances=True,
                )

                status = response["InstanceStatuses"][0]
                instance_state = status["InstanceState"]["Name"]

                if instance_state == "running":
                    # Check if status checks passed
                    system_status = status.get("SystemStatus", {}).get("Status", "")
                    instance_status = status.get("InstanceStatus", {}).get("Status", "")

                    if system_status == "ok" and instance_status == "ok":
                        return True

                time.sleep(check_interval)

            except ClientError as e:
                raise RuntimeError(f"Failed to check instance status: {e}") from e

        return False
