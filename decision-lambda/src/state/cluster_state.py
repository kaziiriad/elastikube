"""Cluster state management and distributed locking.

This module provides:
- Cluster state: Current node count, scaling status, cooldowns
- Distributed lock: Prevents concurrent scaling operations
- Uses DynamoDB conditional writes for atomic operations
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from utils.config import get_config


@dataclass
class ClusterState:
    """Current state of the K3s cluster."""

    cluster_id: str
    node_count: int
    scaling_in_progress: bool
    last_scale_time: Optional[str] = None  # ISO timestamp
    last_scale_operation: Optional[str] = None  # SCALE_UP or SCALE_DOWN
    ttl: int = field(default_factory=lambda: int(
        (datetime.utcnow() + timedelta(hours=24)).timestamp()
    ))

    def to_dict(self) -> dict:
        """Convert to DynamoDB format."""
        return {
            "cluster_id": self.cluster_id,
            "node_count": self.node_count,
            "scaling_in_progress": "true" if self.scaling_in_progress else "false",
            "last_scale_time": self.last_scale_time or "",
            "last_scale_operation": self.last_scale_operation or "",
            "ttl": self.ttl,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClusterState":
        """Create from DynamoDB format."""
        return cls(
            cluster_id=data["cluster_id"],
            node_count=int(data.get("node_count", 0)),
            scaling_in_progress=data.get("scaling_in_progress", "false").lower() == "true",
            last_scale_time=data.get("last_scale_time"),
            last_scale_operation=data.get("last_scale_operation"),
            ttl=int(data.get("ttl", 0)),
        )

    def is_in_cooldown(self, cooldown_seconds: int) -> bool:
        """Check if cluster is in cooldown period."""
        if not self.last_scale_time:
            return False

        try:
            last_time = datetime.fromisoformat(self.last_scale_time)
            elapsed = (datetime.utcnow() - last_time).total_seconds()
            return elapsed < cooldown_seconds
        except (ValueError, TypeError):
            return False


class DistributedLock:
    """Distributed lock using DynamoDB conditional writes.

    Prevents multiple Lambda invocations from scaling simultaneously.
    Uses optimistic locking with conditional expressions.
    """

    def __init__(self, dynamodb_client=None):
        """Initialize the lock manager.

        Args:
            dynamodb_client: Optional boto3 DynamoDB client
        """
        self._client = dynamodb_client or boto3.client("dynamodb")
        self._config = get_config()
        self._table_name = self._config.state_table_name
        self._cluster_id = self._config.cluster_name

    def acquire(self, timeout_seconds: int = 60) -> bool:
        """Acquire the distributed lock.

        Uses conditional write to atomically set scaling_in_progress=true
        only if it's currently false.

        Args:
            timeout_seconds: Maximum time to wait for lock (default: 60s)

        Returns:
            True if lock acquired, False if already held
        """
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            try:
                self._client.update_item(
                    TableName=self._table_name,
                    Key={"cluster_id": {"S": self._cluster_id}},
                    UpdateExpression="SET scaling_in_progress = :true",
                    ConditionExpression="scaling_in_progress = :false OR attribute_not_exists(scaling_in_progress)",
                    ExpressionAttributeValues={
                        ":true": {"S": "true"},
                        ":false": {"S": "false"},
                    },
                )
                return True

            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    # Lock held by another process
                    time.sleep(1)
                    continue
                raise RuntimeError(f"Failed to acquire lock: {e}") from e

        return False  # Timeout

    def release(self) -> None:
        """Release the distributed lock."""
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"cluster_id": {"S": self._cluster_id}},
                UpdateExpression="SET scaling_in_progress = :false",
                ExpressionAttributeValues={
                    ":false": {"S": "false"},
                },
            )
        except ClientError as e:
            raise RuntimeError(f"Failed to release lock: {e}") from e

    def __enter__(self):
        """Context manager entry."""
        if not self.acquire():
            raise RuntimeError("Could not acquire distributed lock")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - always releases lock."""
        self.release()


class StateManager:
    """Manages cluster state in DynamoDB."""

    def __init__(self, dynamodb_client=None):
        """Initialize the state manager.

        Args:
            dynamodb_client: Optional boto3 DynamoDB client
        """
        self._client = dynamodb_client or boto3.client("dynamodb")
        self._config = get_config()
        self._table_name = self._config.state_table_name
        self._cluster_id = self._config.cluster_name

    def get_state(self) -> ClusterState:
        """Get current cluster state.

        Returns:
            Current cluster state or default if not exists
        """
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key={"cluster_id": {"S": self._cluster_id}},
            )

            if "Item" not in response:
                # Return default state
                return ClusterState(
                    cluster_id=self._cluster_id,
                    node_count=0,
                    scaling_in_progress=False,
                )

            # Deserialize DynamoDB typed format to plain dict
            from boto3.dynamodb.types import TypeDeserializer
            deserializer = TypeDeserializer()
            plain_data = deserializer.deserialize({"M": response["Item"]})

            return ClusterState.from_dict(plain_data)

        except ClientError as e:
            raise RuntimeError(f"Failed to get cluster state: {e}")

    def update_state(
        self,
        node_count: Optional[int] = None,
        scaling_in_progress: Optional[bool] = None,
        last_scale_operation: Optional[str] = None,
    ) -> ClusterState:
        """Update cluster state.

        Args:
            node_count: New node count
            scaling_in_progress: Scaling status
            last_scale_operation: Last operation type (SCALE_UP/SCALE_DOWN)

        Returns:
            Updated cluster state from DynamoDB
        """
        # Build update expression
        update_expressions = []
        expression_values = {}

        if node_count is not None:
            update_expressions.append("node_count = :node_count")
            expression_values[":node_count"] = {"N": str(node_count)}

        if scaling_in_progress is not None:
            update_expressions.append("scaling_in_progress = :scaling_in_progress")
            expression_values[":scaling_in_progress"] = {
                "S": "true" if scaling_in_progress else "false"
            }

        if last_scale_operation is not None:
            update_expressions.append("last_scale_operation = :last_scale_operation")
            expression_values[":last_scale_operation"] = {"S": last_scale_operation}
            update_expressions.append("last_scale_time = :last_scale_time")
            from datetime import timezone
            expression_values[":last_scale_time"] = {
                "S": datetime.now(timezone.utc).isoformat()
            }

        if not update_expressions:
            # No updates requested, return current state
            return self.get_state()

        try:
            result = self._client.update_item(
                TableName=self._table_name,
                Key={"cluster_id": {"S": self._cluster_id}},
                UpdateExpression="SET " + ", ".join(update_expressions),
                ExpressionAttributeValues=expression_values,
                ReturnValues="ALL_NEW",
            )

            # Parse and return the updated state from DynamoDB
            attributes = result["Attributes"]
            return ClusterState(
                cluster_id=attributes["cluster_id"]["S"],
                node_count=int(attributes["node_count"]["N"]),
                scaling_in_progress=attributes["scaling_in_progress"]["S"] == "true",
                last_scale_time=attributes.get("last_scale_time", {}).get("S", ""),
                last_scale_operation=attributes.get("last_scale_operation", {}).get("S", ""),
            )

        except ClientError as e:
            raise RuntimeError(f"Failed to update cluster state: {e}") from e
