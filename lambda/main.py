"""K3s Autoscaler Lambda handler.

Orchestrates the autoscaling workflow:
1. Acquire distributed lock
2. Query Prometheus for cluster metrics
3. Evaluate scaling decision
4. Execute scale-up or scale-down operations
5. Update cluster state and release lock

Environment variables required:
    CLUSTER_NAME: Name of the K3s cluster
    PROMETHEUS_URL: Prometheus NodePort URL (http://<worker-ip>:30900)
    STATE_TABLE_NAME: DynamoDB table for cluster state
    WAL_TABLE_NAME: DynamoDB table for write-ahead log
    MIN_NODES / MAX_NODES: Scaling limits
    SCALE_UP_THRESHOLD / SCALE_DOWN_THRESHOLD: CPU thresholds
    SCALE_UP_COOLDOWN / SCALE_DOWN_COOLDOWN: Cooldown periods
    DRY_RUN: If true, simulate without changes
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Add src to path for Lambda deployment
sys.path.insert(0, str(Path(__file__).parent / "src"))

from metrics.prometheus import PrometheusClient, ClusterMetrics
from scaler.scaling import ScalingEngine, ScalingDecision, ScalingAction
from scaler.ec2 import EC2Operations
from state.cluster_state import StateManager, DistributedLock
from utils.cluster_credentials import get_cluster_credentials, generate_user_data_script
from utils.wal import WriteAheadLog, OperationType, OperationState

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


def lambda_handler(event: dict, context: Any) -> dict:
    """Lambda entry point for K3s autoscaler.

    Args:
        event: Lambda event (not used for EventBridge trigger)
        context: Lambda context

    Returns:
        Response with scaling decision and status
    """
    logger.info("K3s Autoscaler Lambda invoked")

    try:
        # Initialize components
        dynamodb_client, ec2_client = get_clients()
        state_manager = StateManager(dynamodb_client)
        lock = DistributedLock(dynamodb_client)
        wal = WriteAheadLog(dynamodb_client)
        prometheus = PrometheusClient()
        scaling_engine = ScalingEngine()
        ec2_ops = EC2Operations(ec2_client)

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
                # TODO: Implement crash recovery logic
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "message": "Incomplete operations found, skipping new scaling",
                        "incomplete_count": len(incomplete),
                    }),
                }

            # Step 4: Fetch cluster metrics
            logger.info("Fetching cluster metrics from Prometheus...")
            metrics = prometheus.get_cluster_metrics()
            logger.info(f"Metrics: CPU={metrics.cpu_percent:.1f}%, "
                       f"Memory={metrics.memory_percent:.1f}%, "
                       f"Pending Pods={metrics.pending_pods}, "
                       f"Nodes={metrics.total_nodes}")

            # Step 5: Evaluate scaling decision
            logger.info("Evaluating scaling decision...")
            decision = scaling_engine.evaluate(metrics, state)
            logger.info(f"Decision: {decision.action.value} - {decision.reason}")

            # Step 6: Execute scaling action
            if decision.action == ScalingAction.SCALE_UP:
                result = _execute_scale_up(ec2_ops, wal, state_manager, decision, metrics)
            elif decision.action == ScalingAction.SCALE_DOWN:
                result = _execute_scale_down(ec2_ops, wal, state_manager, decision)
            else:
                result = {"message": decision.reason}

            # Step 7: Update cluster state
            updated_state = state_manager.update_state(
                node_count=metrics.total_nodes,
                scaling_in_progress=False,
            )

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


def _execute_scale_up(
    ec2_ops: EC2Operations,
    wal: WriteAheadLog,
    state_manager: StateManager,
    decision: ScalingDecision,
    metrics: ClusterMetrics,
) -> dict:
    """Execute scale-up operation.

    Args:
        ec2_ops: EC2 operations client
        wal: Write-ahead log
        state_manager: State manager
        decision: Scaling decision
        metrics: Cluster metrics

    Returns:
        Result dictionary
    """
    logger.info("Executing SCALE_UP operation")

    # Create WAL entry
    wal_entry = wal.create_entry(OperationType.SCALE_UP)
    logger.info(f"Created WAL entry: {wal_entry.operation_id}")

    # Update state: scaling in progress
    state_manager.update_state(scaling_in_progress=True, last_scale_operation="SCALE_UP")

    # Fetch cluster credentials for node join
    logger.info("Fetching cluster credentials from AWS...")
    credentials = get_cluster_credentials()
    if not credentials:
        raise RuntimeError("Failed to fetch cluster credentials - ensure cluster is properly initialized")

    logger.info(f"Retrieved credentials: {credentials}")

    # Generate user-data script for K3s join
    user_data_script = generate_user_data_script(credentials)
    logger.info("Generated user-data script for K3s worker bootstrap")

    # Get EC2 configuration from environment (set by Pulumi)
    from utils.config import get_config
    config = get_config()

    subnet_id = config.subnet_id
    security_group_id = config.security_group_id
    iam_instance_profile = config.iam_instance_profile
    ami_id = config.ami_id
    instance_type = config.instance_type

    # Validate required configuration
    if not all([subnet_id, security_group_id, iam_instance_profile, ami_id]):
        missing = [
            name for name, val in [
                ("SUBNET_ID", subnet_id),
                ("SECURITY_GROUP_ID", security_group_id),
                ("IAM_INSTANCE_PROFILE", iam_instance_profile),
                ("AMI_ID", ami_id),
            ] if not val
        ]
        raise RuntimeError(f"Missing required EC2 configuration: {', '.join(missing)}")

    logger.info(f"EC2 config: subnet={subnet_id}, sg={security_group_id}, "
                f"instance_profile={iam_instance_profile}, ami={ami_id}, type={instance_type}")

    try:
        # Launch new instance with user-data script
        instance_id = ec2_ops.launch_worker(
            subnet_id=subnet_id,
            security_group_id=security_group_id,
            iam_instance_profile=iam_instance_profile,
            ami_id=ami_id,
            instance_type=instance_type,
            user_data=user_data_script,
        )
        logger.info(f"Launched new instance: {instance_id}")

        # Wait for instance to be ready
        # TODO: Configure timeout
        if ec2_ops.wait_for_instance_ready(instance_id):
            logger.info(f"Instance {instance_id} is ready")
        else:
            logger.warning(f"Instance {instance_id} readiness timeout")

        # TODO: Wait for K3s node to join and be Ready
        # This requires kubectl access to the cluster

        # Update WAL: succeeded
        wal.update_entry(wal_entry.operation_id, OperationState.SUCCEEDED)

        # Update state: increment node count
        new_state = state_manager.update_state(
            node_count=decision.current_nodes + 1,
            scaling_in_progress=False,
            last_scale_operation="SCALE_UP",
        )

        return {
            "instance_id": instance_id,
            "wal_entry_id": wal_entry.operation_id,
            "new_node_count": new_state.node_count,
        }

    except Exception as e:
        logger.exception(f"Scale-up failed: {e}")
        wal.update_entry(
            wal_entry.operation_id,
            OperationState.FAILED,
            error_message=str(e),
        )
        raise


def _execute_scale_down(
    ec2_ops: EC2Operations,
    wal: WriteAheadLog,
    state_manager: StateManager,
    decision: ScalingDecision,
) -> dict:
    """Execute scale-down operation.

    Args:
        ec2_ops: EC2 operations client
        wal: Write-ahead log
        state_manager: State manager
        decision: Scaling decision

    Returns:
        Result dictionary
    """
    logger.info("Executing SCALE_DOWN operation")

    # Create WAL entry
    wal_entry = wal.create_entry(OperationType.SCALE_DOWN)
    logger.info(f"Created WAL entry: {wal_entry.operation_id}")

    # Update state: scaling in progress
    state_manager.update_state(scaling_in_progress=True, last_scale_operation="SCALE_DOWN")

    try:
        # Select instance to terminate (LIFO)
        instance = ec2_ops.get_instance_for_scale_down()
        if not instance:
            logger.warning("No eligible instance found for scale-down")
            return {"message": "No eligible instance to terminate"}

        instance_id = instance["InstanceId"]
        logger.info(f"Selected instance for termination: {instance_id}")

        # TODO: Execute kubectl drain before termination
        # This requires kubectl access to the cluster

        # Terminate instance
        ec2_ops.terminate_instance(instance_id)
        logger.info(f"Terminated instance: {instance_id}")

        # Update WAL: succeeded
        wal.update_entry(wal_entry.operation_id, OperationState.SUCCEEDED)

        # Update state: decrement node count
        new_state = state_manager.update_state(
            node_count=decision.current_nodes - 1,
            scaling_in_progress=False,
            last_scale_operation="SCALE_DOWN",
        )

        return {
            "instance_id": instance_id,
            "wal_entry_id": wal_entry.operation_id,
            "new_node_count": new_state.node_count,
        }

    except Exception as e:
        logger.exception(f"Scale-down failed: {e}")
        wal.update_entry(
            wal_entry.operation_id,
            OperationState.FAILED,
            error_message=str(e),
        )
        raise