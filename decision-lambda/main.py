"""K3s Autoscaler Lambda handler.

Orchestrates the autoscaling workflow using Lambda chaining:
1. Acquire distributed lock
2. Query Prometheus for cluster metrics
3. Evaluate scaling decision
4. Publish scaling event to EventBridge (triggers execution Lambda)
5. Release lock

Environment variables required:
    CLUSTER_NAME: Name of the K3s cluster
    PROMETHEUS_URL: Prometheus NodePort URL (http://<worker-ip>:30900)
    STATE_TABLE_NAME: DynamoDB table for cluster state
    WAL_TABLE_NAME: DynamoDB table for write-ahead log
    EVENT_BUS_NAME: EventBridge event bus name (default: default)
    MIN_NODES / MAX_NODES: Scaling limits
    SCALE_UP_THRESHOLD / SCALE_DOWN_THRESHOLD: CPU thresholds
    SCALE_UP_COOLDOWN / SCALE_DOWN_COOLDOWN: Cooldown periods
    DRY_RUN: If true, simulate without changes
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Add src to path for Lambda deployment
sys.path.insert(0, str(Path(__file__).parent / "src"))

from metrics.prometheus import PrometheusClient, ClusterMetrics
from scaler.scaling import ScalingEngine, ScalingDecision, ScalingAction
from state.cluster_state import StateManager, DistributedLock
from utils.wal import WriteAheadLog, OperationType, OperationState
from data.recorder import DynamoDBRecorder

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized on cold start)
dynamodb_client = None
ec2_client = None


def get_clients():
    """Initialize boto3 clients (lazy initialization)."""
    global dynamodb_client, ec2_client
    if dynamodb_client is None:
        dynamodb_client = boto3.client("dynamodb")
    if ec2_client is None:
        ec2_client = boto3.client("ec2")
    return dynamodb_client, ec2_client


def _publish_cloudwatch_metrics(metrics: ClusterMetrics, node_count: int) -> None:
    """Publish cluster metrics to CloudWatch using PutMetricData API.

    This sends metrics directly to CloudWatch without embedding in logs.

    Args:
        metrics: Cluster metrics from Prometheus
        node_count: Current node count
    """
    import boto3

    cloudwatch = boto3.client("cloudwatch")

    # Build metric data list
    metric_data = [
        # Worker metrics
        {
            "MetricName": "WorkerCPU",
            "Value": metrics.worker_cpu_percent_avg,
            "Unit": "Percent",
            "Dimensions": [{"Name": "Cluster", "Value": "k3s-cluster"}],
        },
        {
            "MetricName": "WorkerMemory",
            "Value": metrics.worker_memory_percent_avg,
            "Unit": "Percent",
            "Dimensions": [{"Name": "Cluster", "Value": "k3s-cluster"}],
        },
        # Master metrics
        {
            "MetricName": "MasterCPU",
            "Value": metrics.master_cpu_percent,
            "Unit": "Percent",
            "Dimensions": [{"Name": "Cluster", "Value": "k3s-cluster"}],
        },
        {
            "MetricName": "MasterMemory",
            "Value": metrics.master_memory_percent,
            "Unit": "Percent",
            "Dimensions": [{"Name": "Cluster", "Value": "k3s-cluster"}],
        },
        # Pod metrics
        {
            "MetricName": "PendingPods",
            "Value": metrics.pending_pods,
            "Unit": "Count",
            "Dimensions": [{"Name": "Cluster", "Value": "k3s-cluster"}],
        },
        # Node metrics
        {
            "MetricName": "WorkerCount",
            "Value": metrics.worker_count,
            "Unit": "Count",
            "Dimensions": [{"Name": "Cluster", "Value": "k3s-cluster"}],
        },
        {
            "MetricName": "ReadyNodes",
            "Value": metrics.ready_nodes,
            "Unit": "Count",
            "Dimensions": [{"Name": "Cluster", "Value": "k3s-cluster"}],
        },
        {
            "MetricName": "TotalNodes",
            "Value": metrics.total_nodes,
            "Unit": "Count",
            "Dimensions": [{"Name": "Cluster", "Value": "k3s-cluster"}],
        },
    ]

    try:
        cloudwatch.put_metric_data(
            Namespace="K3sAutoscaler",
            MetricData=metric_data,
        )
        logger.debug(f"Published {len(metric_data)} metrics to CloudWatch")
    except Exception as e:
        logger.warning(f"Failed to publish CloudWatch metrics: {e}")


def _handle_health_check() -> dict:
    """Handle health check requests.

    Returns the health status of all autoscaler components.

    Returns:
        Health check response with component statuses
    """
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": {},
    }

    try:
        dynamodb_client, _ = get_clients()

        # Check DynamoDB connection and cluster state
        try:
            state_manager = StateManager(dynamodb_client)
            state = state_manager.get_state()
            health_status["components"]["dynamodb"] = {
                "status": "healthy",
                "cluster_name": state.cluster_id,
                "node_count": state.node_count,
            }
        except Exception as e:
            health_status["components"]["dynamodb"] = {
                "status": "unhealthy",
                "error": str(e),
            }
            health_status["status"] = "degraded"

        # Check distributed lock
        try:
            lock = DistributedLock(dynamodb_client)
            lock_info = lock.get_lock_info()
            health_status["components"]["distributed_lock"] = {
                "status": "healthy" if not lock_info else "locked",
                "locked": bool(lock_info),
                "lock_age_seconds": lock_info.get("age_seconds") if lock_info else 0,
            }
        except Exception as e:
            health_status["components"]["distributed_lock"] = {
                "status": "unhealthy",
                "error": str(e),
            }
            health_status["status"] = "degraded"

        # Check WAL
        try:
            wal = WriteAheadLog(dynamodb_client)
            recent_operations = wal.get_recent_operations(limit=1)
            health_status["components"]["wal"] = {
                "status": "healthy",
                "recent_operations": len(recent_operations),
            }
        except Exception as e:
            health_status["components"]["wal"] = {
                "status": "unhealthy",
                "error": str(e),
            }
            health_status["status"] = "degraded"

        # Check Prometheus connectivity
        try:
            prometheus = PrometheusClient()
            health_status["components"]["prometheus"] = {
                "status": "healthy",
                "url": os.environ.get("PROMETHEUS_URL", "not configured"),
            }
        except Exception as e:
            health_status["components"]["prometheus"] = {
                "status": "unhealthy",
                "error": str(e),
            }
            health_status["status"] = "degraded"

    except Exception as e:
        health_status["status"] = "unhealthy"
        health_status["error"] = str(e)

    return {
        "statusCode": 200 if health_status["status"] in ("healthy", "degraded") else 503,
        "body": json.dumps(health_status, indent=2),
    }


def lambda_handler(event: dict, context: Any) -> dict:
    """Lambda entry point for K3s autoscaler.

    Args:
        event: Lambda event
            - {"action": "health_check"} for health check
            - EventBridge trigger for autoscaling
        context: Lambda context

    Returns:
        Response with scaling decision, health status, or error
    """
    # Health check endpoint
    if event.get("action") == "health_check":
        return _handle_health_check()

    logger.info("K3s Autoscaler Lambda invoked")

    try:
        # Initialize components
        dynamodb_client, ec2_client = get_clients()
        state_manager = StateManager(dynamodb_client)
        lock = DistributedLock(dynamodb_client)
        wal = WriteAheadLog(dynamodb_client)
        prometheus = PrometheusClient()
        scaling_engine = ScalingEngine()
        recorder = DynamoDBRecorder(dynamodb_client)

        # Step 1: Fetch cluster state (before lock to detect stuck locks)
        logger.info("Fetching cluster state...")
        state = state_manager.get_state()
        logger.info(f"Cluster state: nodes={state.node_count}, "
                   f"scaling_in_progress={state.scaling_in_progress}")

        # Step 2: Acquire distributed lock
        logger.info("Acquiring distributed lock...")
        if not lock.acquire(timeout_seconds=10):
            logger.warning("Could not acquire lock - another instance is running")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "Another autoscaler run is in progress",
                    "action": "NO_OP",
                }),
            }
        logger.info("Lock acquired")

        try:

            # Step 3: Check for incomplete operations (crash recovery)
            incomplete = wal.get_incomplete_operations()
            if incomplete:
                logger.warning(f"Found {len(incomplete)} incomplete operations")
                for entry in incomplete:
                    started_at = datetime.fromisoformat(entry.started_at)
                    # Add UTC timezone if not present
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
                    logger.warning(f"  - Operation: {entry.operation_id}, Type: {entry.operation_type.value}, "
                                 f"Age: {age_seconds:.0f}s, Started: {entry.started_at}")
                    # Mark stale operations (older than 10 minutes) as FAILED
                    if age_seconds > 600:  # 10 minutes
                        logger.info(f"Marking stale operation {entry.operation_id} as FAILED")
                        wal.update_entry(
                            entry.operation_id,
                            OperationState.FAILED,
                            error_message=f"Crash recovery: operation stalled for {age_seconds:.0f}s",
                            started_at=entry.started_at,
                        )
                # Re-check incomplete operations after cleanup
                incomplete = wal.get_incomplete_operations()
                if incomplete:
                    logger.warning(f"Still have {len(incomplete)} incomplete operations (may be recent)")
                    # Only return if incomplete operations are still recent (< 10 minutes)
                    return {
                        "statusCode": 200,
                        "body": json.dumps({
                            "message": "Recent incomplete operations found, skipping new scaling",
                            "incomplete_count": len(incomplete),
                        }),
                    }
                logger.info("All stale incomplete operations cleaned up, proceeding with scaling")

            # Step 4: Fetch cluster metrics
            logger.info("Fetching cluster metrics from Prometheus...")
            metrics = prometheus.get_cluster_metrics()

            # Step 4.5: Record periodic metrics sample for ML training
            recorder.record_metrics_sample(metrics)

            # Log to console
            logger.info(f"Worker Metrics: CPU={metrics.worker_cpu_percent_avg:.1f}%, "
                       f"Memory={metrics.worker_memory_percent_avg:.1f}%, "
                       f"Count={metrics.worker_count}")
            logger.info(f"Master Metrics: CPU={metrics.master_cpu_percent:.1f}%, "
                       f"Memory={metrics.master_memory_percent:.1f}%")
            logger.info(f"Cluster: Pending Pods={metrics.pending_pods}, "
                       f"Ready Nodes={metrics.ready_nodes}/{metrics.total_nodes}")

            # Publish metrics to CloudWatch using Embedded Metric Format
            _publish_cloudwatch_metrics(metrics, state.node_count)

            # Step 5: Evaluate scaling decision
            logger.info("Evaluating scaling decision...")
            decision = scaling_engine.evaluate(metrics, state)
            logger.info(f"Decision: {decision.action.value} - {decision.reason}")

            # Step 5.5: Record scaling decision for ML training and audit trail
            recorder.record_decision(decision, metrics)

            # Step 6: Execute scaling action via EventBridge
            if decision.action == ScalingAction.SCALE_UP:
                result = _publish_scale_up_event(decision)
            elif decision.action == ScalingAction.SCALE_DOWN:
                result = _publish_scale_down_event(decision)
            else:
                result = {"message": decision.reason}

            # Always sync node_count from actual cluster state (Prometheus/Kubernetes API)
            # Use ready_nodes instead of total_nodes to exclude NotReady nodes from count
            # This prevents state desync when nodes fail to join or leave unexpectedly
            # Note: last_scale_operation and last_scale_time are set by scale-up/scale-down Lambdas
            # after they complete their operations, not here during decision making
            update_kwargs = {
                "node_count": metrics.ready_nodes,
                "scaling_in_progress": False,
            }

            _ = state_manager.update_state(**update_kwargs)

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "current_nodes": decision.current_nodes,
                    "target_nodes": decision.target_nodes,
                    "metrics": {
                        "cpu_percent": decision.cpu_percent,
                        "memory_percent": decision.memory_percent,
                        "pending_pods": decision.pending_pods,
                    },
                    "result": result,
                }),
            }

        finally:
            # Always release the lock
            lock.release()
            logger.info("Released distributed lock")

    except Exception as e:
        logger.exception("Lambda execution failed")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": str(e),
            }),
        }


def _publish_scale_up_event(decision: ScalingDecision) -> dict:
    """Publish scale-up event to EventBridge.

    EventBridge will route this to the scale-up Lambda with built-in retry and DLQ.

    Args:
        decision: Scaling decision containing context

    Returns:
        Result dictionary with publication status
    """
    import os
    import boto3

    events_client = boto3.client("events")
    event_bus_name = os.environ.get("EVENT_BUS_NAME", "default")
    cluster_name = os.environ.get("CLUSTER_NAME", "production-k3s")

    logger.info(f"Publishing scale-up event to EventBridge: {event_bus_name}")

    try:
        response = events_client.put_events(
            Entries=[
                {
                    "Source": "k3s.autoscaler",
                    "DetailType": "ScaleUp",
                    "Detail": json.dumps({
                        "cluster_name": cluster_name,
                        "current_nodes": decision.current_nodes,
                        "target_nodes": decision.target_nodes,
                        "reason": decision.reason,
                        "cpu_percent": decision.cpu_percent,
                        "memory_percent": decision.memory_percent,
                        "pending_pods": decision.pending_pods,
                    }),
                    "EventBusName": event_bus_name,
                }
            ]
        )

        # put_events returns a list of entries (one per event)
        entry_response = response.get("Entries", [{}])[0]
        if entry_response.get("EventId"):
            logger.info(f"✓ Scale-up event published: {entry_response['EventId']}")
            return {
                "status": "published",
                "event_id": entry_response["EventId"],
                "event_bus": event_bus_name,
            }
        else:
            logger.warning("Event published but no EventId returned")
            return {
                "status": "unknown",
                "event_bus": event_bus_name,
            }

    except Exception as e:
        logger.exception(f"Failed to publish scale-up event: {e}")
        return {
            "status": "failed",
            "error": str(e),
            "event_bus": event_bus_name,
        }


def _publish_scale_down_event(decision: ScalingDecision) -> dict:
    """Publish scale-down event to EventBridge.

    EventBridge will route this to the scale-down Lambda with built-in retry and DLQ.

    Args:
        decision: Scaling decision containing context

    Returns:
        Result dictionary with publication status
    """
    import os
    import boto3

    events_client = boto3.client("events")
    event_bus_name = os.environ.get("EVENT_BUS_NAME", "default")
    cluster_name = os.environ.get("CLUSTER_NAME", "production-k3s")

    logger.info(f"Publishing scale-down event to EventBridge: {event_bus_name}")

    try:
        response = events_client.put_events(
            Entries=[
                {
                    "Source": "k3s.autoscaler",
                    "DetailType": "ScaleDown",
                    "Detail": json.dumps({
                        "cluster_name": cluster_name,
                        "current_nodes": decision.current_nodes,
                        "target_nodes": decision.target_nodes,
                        "reason": decision.reason,
                        "cpu_percent": decision.cpu_percent,
                        "memory_percent": decision.memory_percent,
                        "pending_pods": decision.pending_pods,
                    }),
                    "EventBusName": event_bus_name,
                }
            ]
        )

        entry_response = response.get("Entries", [{}])[0]
        if entry_response.get("EventId"):
            logger.info(f"✓ Scale-down event published: {entry_response['EventId']}")
            return {
                "status": "published",
                "event_id": entry_response["EventId"],
                "event_bus": event_bus_name,
            }
        else:
            logger.warning("Event published but no EventId returned")
            return {
                "status": "unknown",
                "event_bus": event_bus_name,
            }

    except Exception as e:
        logger.exception(f"Failed to publish scale-down event: {e}")
        return {
            "status": "failed",
            "error": str(e),
            "event_bus": event_bus_name,
        }