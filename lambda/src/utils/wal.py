"""Write-Ahead Log for autoscaling operations.

The WAL provides:
- Crash recovery: Lambda can resume incomplete operations
- Audit trail: Complete history of scaling decisions
- Idempotency: Check operation state before re-executing
"""

import json
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Literal

import boto3
from botocore.exceptions import ClientError

from .config import get_config


class OperationState(str, Enum):
    """States for scaling operations."""

    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class OperationType(str, Enum):
    """Types of scaling operations."""

    SCALE_UP = "SCALE_UP"
    SCALE_DOWN = "SCALE_DOWN"


@dataclass
class WalEntry:
    """A WAL entry representing a scaling operation."""

    operation_id: str
    started_at: str  # ISO timestamp
    state: OperationState
    operation_type: OperationType
    node_id: Optional[str] = None  # EC2 instance ID
    node_name: Optional[str] = None  # K8s node name
    error_message: Optional[str] = None
    completed_at: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to DynamoDB format."""
        data = asdict(self)
        data["state"] = self.state.value
        data["operation_type"] = self.operation_type.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "WalEntry":
        """Create from DynamoDB format."""
        return cls(
            operation_id=data["operation_id"],
            started_at=data["started_at"],
            state=OperationState(data["state"]),
            operation_type=OperationType(data["operation_type"]),
            node_id=data.get("node_id"),
            node_name=data.get("node_name"),
            error_message=data.get("error_message"),
            completed_at=data.get("completed_at"),
        )


class WriteAheadLog:
    """Manages write-ahead log entries in DynamoDB."""

    def __init__(self, dynamodb_client=None):
        """Initialize the WAL.

        Args:
            dynamodb_client: Optional boto3 DynamoDB client
        """
        self._client = dynamodb_client or boto3.client("dynamodb")
        self._config = get_config()
        self._table_name = self._config.wal_table_name

    def create_entry(
        self,
        operation_type: OperationType,
        node_id: Optional[str] = None,
        node_name: Optional[str] = None,
    ) -> WalEntry:
        """Create a new WAL entry.

        Args:
            operation_type: Type of operation (SCALE_UP/SCALE_DOWN)
            node_id: EC2 instance ID
            node_name: Kubernetes node name

        Returns:
            The created WAL entry
        """
        entry = WalEntry(
            operation_id=str(uuid.uuid4()),
            started_at=datetime.utcnow().isoformat(),
            state=OperationState.STARTED,
            operation_type=operation_type,
            node_id=node_id,
            node_name=node_name,
        )

        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=self._to_dynamodb_item(entry.to_dict()),
            )
            return entry
        except ClientError as e:
            raise RuntimeError(f"Failed to create WAL entry: {e}") from e

    def update_entry(
        self,
        operation_id: str,
        state: OperationState,
        error_message: Optional[str] = None,
    ) -> WalEntry:
        """Update an existing WAL entry.

        Args:
            operation_id: Operation ID to update
            state: New state
            error_message: Optional error message if FAILED

        Returns:
            The updated WAL entry
        """
        # Get current entry
        current = self.get_entry(operation_id)
        if not current:
            raise ValueError(f"WAL entry {operation_id} not found")

        # Update fields
        current.state = state
        current.error_message = error_message
        if state in (OperationState.SUCCEEDED, OperationState.FAILED):
            current.completed_at = datetime.utcnow().isoformat()

        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=self._to_dynamodb_item(current.to_dict()),
            )
            return current
        except ClientError as e:
            raise RuntimeError(f"Failed to update WAL entry: {e}") from e

    def get_entry(self, operation_id: str) -> Optional[WalEntry]:
        """Get a WAL entry by ID.

        Args:
            operation_id: Operation ID to fetch

        Returns:
            The WAL entry or None if not found
        """
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key={
                    "operation_id": {"S": operation_id},
                },
            )
            if "Item" not in response:
                return None
            return WalEntry.from_dict(self._from_dynamodb_item(response["Item"]))
        except ClientError as e:
            raise RuntimeError(f"Failed to get WAL entry: {e}") from e

    def get_incomplete_operations(self) -> list[WalEntry]:
        """Get all incomplete operations for crash recovery.

        Returns:
            List of WAL entries in STARTED state
        """
        try:
            response = self._client.query(
                TableName=self._table_name,
                IndexName="IncompleteOperations",
                KeyConditionExpression="#state = :state",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":state": {"S": OperationState.STARTED.value}},
            )
            return [
                WalEntry.from_dict(self._from_dynamodb_item(item))
                for item in response.get("Items", [])
            ]
        except ClientError as e:
            raise RuntimeError(f"Failed to query incomplete operations: {e}") from e

    @staticmethod
    def _to_dynamodb_item(data: dict) -> dict:
        """Convert dict to DynamoDB item format."""
        return {
            k: {"S": v} if isinstance(v, str) else
               {"N": str(v)} if isinstance(v, (int, float)) else
               {"NULL": True} if v is None else
               {"BOOL": v} if isinstance(v, bool) else
               {"M": WriteAheadLog._to_dynamodb_item(v) if isinstance(v, dict) else v}
            for k, v in data.items()
        }

    @staticmethod
    def _from_dynamodb_item(item: dict) -> dict:
        """Convert DynamoDB item format to dict."""
        return {
            k: v.get("S") if "S" in v else
               float(v["N"]) if "N" in v else
               None if "NULL" in v else
               v["BOOL"] if "BOOL" in v else
               WriteAheadLog._from_dynamodb_item(v["M"]) if "M" in v else v
            for k, v in item.items()
        }
