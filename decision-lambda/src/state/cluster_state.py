"""Cluster state management and distributed locking.

This module provides:
- Cluster state: Current node count, scaling status, cooldowns
- Distributed lock: Prevents concurrent scaling operations
- Uses DynamoDB conditional writes for atomic operations
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from utils.config import get_config

logger = logging.getLogger(__name__)

# A scaling_in_progress flag older than this is treated as abandoned.
# Must exceed the longest Lambda timeout that sets the flag (300s in
# infrastructure/pulumi/__main__.py), so a live holder is never preempted.
LOCK_STALE_SECONDS = 360


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
            # Scale Lambdas write tz-aware UTC; treat naive values as UTC.
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()
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
        self._owner = f"decision-{uuid.uuid4()}"

    def acquire(self, timeout_seconds: int = 60) -> bool:
        """Acquire the distributed lock.

        Atomically sets scaling_in_progress=true (plus owner and timestamp)
        when the flag is free, or when the current holder is presumed dead:
        its scaling_in_progress_since is older than LOCK_STALE_SECONDS, or it
        has no timestamp at all (legacy writes that never recorded one).

        The flag is shared with the scale-up Lambda, which writes the same
        scaling_in_progress_since attribute, so a crashed scale-up is also
        recovered here.

        Args:
            timeout_seconds: Maximum time to wait for lock (default: 60s)

        Returns:
            True if lock acquired, False if already held
        """
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            now = datetime.now(timezone.utc)
            stale_cutoff = now - timedelta(seconds=LOCK_STALE_SECONDS)
            try:
                self._client.update_item(
                    TableName=self._table_name,
                    Key={"cluster_id": {"S": self._cluster_id}},
                    UpdateExpression=(
                        "SET scaling_in_progress = :true, "
                        "scaling_in_progress_since = :now, "
                        "lock_owner = :owner"
                    ),
                    # ISO-8601 UTC strings with the same offset sort
                    # chronologically, so a string compare is safe here.
                    ConditionExpression=(
                        "attribute_not_exists(scaling_in_progress) "
                        "OR scaling_in_progress = :false "
                        "OR attribute_not_exists(scaling_in_progress_since) "
                        "OR scaling_in_progress_since < :stale_cutoff"
                    ),
                    ExpressionAttributeValues={
                        ":true": {"S": "true"},
                        ":false": {"S": "false"},
                        ":now": {"S": now.isoformat()},
                        ":owner": {"S": self._owner},
                        ":stale_cutoff": {"S": stale_cutoff.isoformat()},
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
        """Release the distributed lock if this instance still owns it.

        If the lock was taken over (we were judged stale), leave it alone so
        we don't release someone else's lock.
        """
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"cluster_id": {"S": self._cluster_id}},
                UpdateExpression=(
                    "SET scaling_in_progress = :false "
                    "REMOVE scaling_in_progress_since, lock_owner"
                ),
                ConditionExpression="lock_owner = :owner",
                ExpressionAttributeValues={
                    ":false": {"S": "false"},
                    ":owner": {"S": self._owner},
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.warning(
                    "Lock no longer owned by %s (taken over as stale); not releasing",
                    self._owner,
                )
                return
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
