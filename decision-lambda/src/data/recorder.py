"""Data recorder for autoscaling history and metrics.

This module provides interfaces and implementations for recording scaling
decisions and periodic metrics samples for ML model training and audit trails.

Following the layered architecture design:
- Layer 1: Data Collection Infrastructure
- Provides extension points for future ML-based predictive scaling
"""

import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

from metrics.prometheus import ClusterMetrics
from scaler.scaling import ScalingDecision

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class ScalingDecisionRecord:
    """Record of a scaling decision for historical tracking.

    Attributes:
        decision_id: Unique identifier for this decision
        timestamp: When the decision was made (UTC ISO format)
        action: The scaling action taken (SCALE_UP, SCALE_DOWN, NO_OP)
        reason: Human-readable explanation of the decision
        current_nodes: Node count before decision
        target_nodes: Desired node count after decision
        cpu_percent: Average worker CPU usage
        memory_percent: Average worker memory usage
        pending_pods: Number of pending pods
        worker_count: Number of worker nodes
        ready_nodes: Number of ready nodes
        total_nodes: Total node count
        ttl: Expiration timestamp for auto-deletion
    """

    decision_id: str
    timestamp: str
    action: str
    reason: str
    current_nodes: int
    target_nodes: int
    cpu_percent: float
    memory_percent: float
    pending_pods: int
    worker_count: int
    ready_nodes: int
    total_nodes: int
    ttl: int

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format."""
        return {
            "decision_id": self.decision_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "reason": self.reason,
            "current_nodes": self.current_nodes,
            "target_nodes": self.target_nodes,
            "cpu_percent": self.cpu_percent,
            "memory_percent": self.memory_percent,
            "pending_pods": self.pending_pods,
            "worker_count": self.worker_count,
            "ready_nodes": self.ready_nodes,
            "total_nodes": self.total_nodes,
            "ttl": self.ttl,
        }

    @classmethod
    def from_decision(
        cls,
        decision: ScalingDecision,
        metrics: ClusterMetrics,
        retention_days: int = 90,
    ) -> "ScalingDecisionRecord":
        """Create record from ScalingDecision and ClusterMetrics.

        Args:
            decision: The scaling decision
            metrics: Cluster metrics at decision time
            retention_days: Days to retain this record (default 90)

        Returns:
            ScalingDecisionRecord instance
        """
        now = datetime.now(timezone.utc)
        ttl_epoch = int((now + timedelta(days=retention_days)).timestamp())

        return cls(
            decision_id=str(uuid.uuid4()),
            timestamp=now.isoformat(),
            action=decision.action.value,
            reason=decision.reason,
            current_nodes=decision.current_nodes,
            target_nodes=decision.target_nodes,
            cpu_percent=decision.cpu_percent,
            memory_percent=decision.memory_percent,
            pending_pods=decision.pending_pods,
            worker_count=metrics.worker_count,
            ready_nodes=metrics.ready_nodes,
            total_nodes=metrics.total_nodes,
            ttl=ttl_epoch,
        )


@dataclass
class MetricsSampleRecord:
    """Periodic metrics sample for pattern analysis.

    Collected at regular intervals regardless of scaling decisions to
    capture baseline patterns and gradual changes.

    Attributes:
        sample_id: Unique identifier for this sample
        timestamp: When the sample was taken (UTC ISO format)
        cpu_percent: Average worker CPU usage
        memory_percent: Average worker memory usage
        pending_pods: Number of pending pods
        worker_count: Number of worker nodes
        ready_nodes: Number of ready nodes
        total_nodes: Total node count
        master_cpu: Master node CPU usage
        master_memory: Master node memory usage
        hour_of_day: Hour (0-23) for time-based pattern analysis
        day_of_week: Day (0-6, Monday=0) for weekly pattern analysis
        ttl: Expiration timestamp for auto-deletion
    """

    sample_id: str
    timestamp: str
    cpu_percent: float
    memory_percent: float
    pending_pods: int
    worker_count: int
    ready_nodes: int
    total_nodes: int
    master_cpu: float
    master_memory: float
    hour_of_day: int
    day_of_week: int
    ttl: int

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format."""
        return {
            "sample_id": self.sample_id,
            "timestamp": self.timestamp,
            "cpu_percent": self.cpu_percent,
            "memory_percent": self.memory_percent,
            "pending_pods": self.pending_pods,
            "worker_count": self.worker_count,
            "ready_nodes": self.ready_nodes,
            "total_nodes": self.total_nodes,
            "master_cpu": self.master_cpu,
            "master_memory": self.master_memory,
            "hour_of_day": self.hour_of_day,
            "day_of_week": self.day_of_week,
            "ttl": self.ttl,
        }

    @classmethod
    def from_metrics(
        cls,
        metrics: ClusterMetrics,
        retention_days: int = 90,
    ) -> "MetricsSampleRecord":
        """Create record from ClusterMetrics.

        Args:
            metrics: Cluster metrics to sample
            retention_days: Days to retain this record (default 90)

        Returns:
            MetricsSampleRecord instance
        """
        now = datetime.now(timezone.utc)
        ttl_epoch = int((now + timedelta(days=retention_days)).timestamp())

        return cls(
            sample_id=str(uuid.uuid4()),
            timestamp=now.isoformat(),
            cpu_percent=metrics.worker_cpu_percent_avg,
            memory_percent=metrics.worker_memory_percent_avg,
            pending_pods=metrics.pending_pods,
            worker_count=metrics.worker_count,
            ready_nodes=metrics.ready_nodes,
            total_nodes=metrics.total_nodes,
            master_cpu=metrics.master_cpu_percent,
            master_memory=metrics.master_memory_percent,
            hour_of_day=now.hour,
            day_of_week=now.weekday(),
            ttl=ttl_epoch,
        )


# =============================================================================
# Data Recorder Interface
# =============================================================================


class DataRecorder(ABC):
    """Abstract interface for recording scaling data.

    This interface allows for different storage backends (DynamoDB, S3, etc.)
    and enables easy testing with mock implementations.

    Extension Point: Future ML models can use this interface to access
    historical data for training and validation.
    """

    @abstractmethod
    def record_decision(
        self,
        decision: ScalingDecision,
        metrics: ClusterMetrics,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record a scaling decision.

        Args:
            decision: The scaling decision made
            metrics: Cluster metrics at decision time
            context: Additional context (optional)

        Returns:
            True if recording succeeded, False otherwise
        """
        pass

    @abstractmethod
    def record_metrics_sample(
        self,
        metrics: ClusterMetrics,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record a periodic metrics sample.

        Args:
            metrics: Cluster metrics to sample
            context: Additional context (optional)

        Returns:
            True if recording succeeded, False otherwise
        """
        pass


# =============================================================================
# DynamoDB Implementation
# =============================================================================


class DynamoDBRecorder(DataRecorder):
    """DynamoDB-based data recorder implementation.

    Stores scaling decisions and metrics samples in DynamoDB tables with
    automatic TTL-based cleanup after configured retention period.

    Environment Variables:
        DECISION_TABLE_NAME: Table for scaling decisions (default: k3s-scaling-history)
        METRICS_TABLE_NAME: Table for metrics samples (default: k3s-scaling-metrics-samples)
        DATA_RETENTION_DAYS: Days to retain data (default: 90)
    """

    def __init__(self, dynamodb_client: Optional[Any] = None):
        """Initialize the DynamoDB recorder.

        Args:
            dynamodb_client: boto3 DynamoDB client (created if not provided)
        """
        self._client = dynamodb_client or boto3.client("dynamodb")
        self._decision_table = os.environ.get(
            "DECISION_TABLE_NAME", "k3s-scaling-history"
        )
        self._metrics_table = os.environ.get(
            "METRICS_TABLE_NAME", "k3s-scaling-metrics-samples"
        )
        self._retention_days = int(
            os.environ.get("DATA_RETENTION_DAYS", "90")
        )

    def record_decision(
        self,
        decision: ScalingDecision,
        metrics: ClusterMetrics,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record a scaling decision to DynamoDB.

        Args:
            decision: The scaling decision made
            metrics: Cluster metrics at decision time
            context: Additional context (not used currently, reserved for future)

        Returns:
            True if recording succeeded, False otherwise
        """
        try:
            record = ScalingDecisionRecord.from_decision(
                decision, metrics, self._retention_days
            )

            self._client.put_item(
                TableName=self._decision_table,
                Item=self._serialize_item(record.to_dynamodb_item()),
            )

            logger.debug(f"Recorded scaling decision: {record.decision_id}")
            return True

        except ClientError as e:
            logger.warning(f"Failed to record scaling decision: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error recording decision: {e}")
            return False

    def record_metrics_sample(
        self,
        metrics: ClusterMetrics,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record a metrics sample to DynamoDB.

        Args:
            metrics: Cluster metrics to sample
            context: Additional context (not used currently, reserved for future)

        Returns:
            True if recording succeeded, False otherwise
        """
        try:
            record = MetricsSampleRecord.from_metrics(
                metrics, self._retention_days
            )

            self._client.put_item(
                TableName=self._metrics_table,
                Item=self._serialize_item(record.to_dynamodb_item()),
            )

            logger.debug(f"Recorded metrics sample: {record.sample_id}")
            return True

        except ClientError as e:
            logger.warning(f"Failed to record metrics sample: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error recording metrics: {e}")
            return False

    @staticmethod
    def _serialize_item(item: Dict[str, Any]) -> Dict[str, Any]:
        """Convert Python types to DynamoDB format.

        Args:
            item: Dictionary with Python native types

        Returns:
            Dictionary with DynamoDB typed values
        """
        dynamo_item = {}

        for key, value in item.items():
            if isinstance(value, str):
                dynamo_item[key] = {"S": value}
            elif isinstance(value, (int, float)):
                dynamo_item[key] = {"N": str(value)}
            elif isinstance(value, bool):
                dynamo_item[key] = {"BOOL": value}
            elif isinstance(value, list):
                dynamo_item[key] = {"L": [
                    DynamoDBRecorder._serialize_value(v) for v in value
                ]}
            elif isinstance(value, dict):
                dynamo_item[key] = {"M": DynamoDBRecorder._serialize_item(value)}
            elif value is None:
                dynamo_item[key] = {"NULL": True}
            else:
                dynamo_item[key] = DynamoDBRecorder._serialize_value(value)

        return dynamo_item

    @staticmethod
    def _serialize_value(value: Any) -> Dict[str, Any]:
        """Serialize a single value to DynamoDB format.

        Args:
            value: Python native value

        Returns:
            DynamoDB typed value
        """
        if isinstance(value, str):
            return {"S": value}
        elif isinstance(value, (int, float)):
            return {"N": str(value)}
        elif isinstance(value, bool):
            return {"BOOL": value}
        elif value is None:
            return {"NULL": True}
        else:
            return {"S": str(value)}
