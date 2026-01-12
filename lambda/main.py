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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Add src to path for Lambda deployment
sys.path.insert(0, str(Path(__file__).parent / "src"))

from metrics.prometheus import PrometheusClient, ClusterMetrics
from scaler.scaling import ScalingEngine, ScalingDecision, ScalingAction
from scaler.ec2 import EC2Operations
from scaler.kubectl import KubectlViaSSM
from state.cluster_state import StateManager, DistributedLock
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

            # Step 6: Execute scaling action
            if decision.action == ScalingAction.SCALE_UP:
                result = _execute_scale_up(ec2_ops, wal, state_manager, decision, metrics)
                # State is already updated inside _execute_scale_up with new_node_count
            elif decision.action == ScalingAction.SCALE_DOWN:
                # Create EC2 and SSM clients for scale-down (kubectl drain via SSM)
                import boto3
                ec2_client = boto3.client("ec2")
                result = _execute_scale_down(ec2_ops, wal, state_manager, decision, ec2_client)
                # State is already updated inside _execute_scale_down with new_node_count
            else:
                result = {"message": decision.reason}
                # For NO_OP, update state to clear scaling_in_progress and sync node count
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

    # Fetch user-data script from S3 (deployed by Ansible worker-bootstrap.yml)
    logger.info("Fetching user-data script from S3...")
    user_data_script = ec2_ops.fetch_user_data_from_s3()
    if not user_data_script:
        raise RuntimeError(
            "Failed to fetch user-data script from S3. "
            "Ensure the worker-bootstrap.yml playbook has been run."
        )
    logger.info("Successfully fetched user-data script for K3s worker bootstrap")

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

        # Wait for K3s node to join and be Ready
        import boto3
        from scaler.kubectl import KubectlViaSSM

        # Create new SSM client for kubectl operations
        ssm_client = boto3.client("ssm")
        ec2_client = boto3.client("ec2")
        kubectl = KubectlViaSSM(ec2_client=ec2_client, ssm_client=ssm_client)

        logger.info(f"Waiting for K3s node to join cluster...")
        node_ready_result = kubectl.wait_for_node_ready(instance_id, timeout=300)

        if node_ready_result["status"] == "Success":
            logger.info(f"✓ Node {node_ready_result['node_name']} joined and Ready "
                       f"in {node_ready_result['elapsed_seconds']}s")
        elif node_ready_result["status"] == "TimedOut":
            logger.error(f"Node did not become Ready in time: {node_ready_result.get('error')}")
            # Still mark as succeeded - EC2 launched successfully, node join may complete later
            logger.warning(f"Proceeding with scale-up completion (node may join asynchronously)")
        else:  # Failed
            logger.error(f"Failed to wait for node ready: {node_ready_result.get('error')}")
            # Still mark as succeeded - EC2 launched successfully
            logger.warning(f"Proceeding with scale-up completion (node status check failed)")

        # Update WAL: succeeded
        wal.update_entry(wal_entry.operation_id, OperationState.SUCCEEDED, started_at=wal_entry.started_at)

        # Update state: increment node count
        new_state = state_manager.update_state(
            node_count=decision.current_nodes + 1,
            scaling_in_progress=False,
            last_scale_operation="SCALE_UP",
        )
        logger.info(f"✓ State updated: node_count={new_state.node_count} (was {decision.current_nodes})")

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
            started_at=wal_entry.started_at,
        )
        raise


def _execute_scale_down(
    ec2_ops: EC2Operations,
    wal: WriteAheadLog,
    state_manager: StateManager,
    decision: ScalingDecision,
    ec2_client,
) -> dict:
    """Execute scale-down operation.

    Args:
        ec2_ops: EC2 operations client
        wal: Write-ahead log
        state_manager: State manager
        decision: Scaling decision
        ec2_client: EC2 client for kubectl operations

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
            # Mark WAL as FAILED and clear scaling_in_progress
            wal.update_entry(
                wal_entry.operation_id,
                OperationState.FAILED,
                error_message="No eligible instance found for scale-down",
                started_at=wal_entry.started_at,
            )
            state_manager.update_state(scaling_in_progress=False)
            return {"message": "No eligible instance to terminate"}

        instance_id = instance["InstanceId"]
        logger.info(f"Selected instance for termination: {instance_id}")

        # Execute kubectl drain before termination via SSM
        kubectl = KubectlViaSSM(ec2_client=ec2_client)
        node_name = kubectl.get_node_name_from_instance_id(instance_id)

        if node_name:
            logger.info(f"Draining Kubernetes node: {node_name}")
            drain_result = kubectl.drain_node(node_name, timeout=120, delete_node=True)

            if drain_result["status"] == "Success":
                logger.info(f"Successfully drained node {node_name}")
            else:
                logger.warning(
                    f"Node drain had issues: {drain_result.get('status', 'Unknown')}. "
                    f"Continuing with termination."
                )
                if drain_result.get("stderr"):
                    logger.warning(f"Drain stderr: {drain_result['stderr']}")
        else:
            logger.warning(
                f"Could not determine node name for instance {instance_id}, "
                f"skipping drain and proceeding with termination"
            )

        # Terminate instance
        ec2_ops.terminate_instance(instance_id)
        logger.info(f"Terminated instance: {instance_id}")

        # Update WAL: succeeded
        wal.update_entry(wal_entry.operation_id, OperationState.SUCCEEDED, started_at=wal_entry.started_at)

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
            started_at=wal_entry.started_at,
        )
        raise