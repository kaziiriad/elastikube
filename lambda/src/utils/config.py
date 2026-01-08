"""Configuration management for K3s autoscaler.

Environment variables:
    CLUSTER_NAME: Name of the K3s cluster
    PROMETHEUS_URL: Prometheus NodePort URL (http://<worker-ip>:30900)
    K3S_MASTER_URL: Kubernetes API URL for drain operations
    STATE_TABLE_NAME: DynamoDB table for cluster state
    WAL_TABLE_NAME: DynamoDB table for write-ahead log
    MIN_NODES: Minimum number of worker nodes
    MAX_NODES: Maximum number of worker nodes
    SCALE_UP_THRESHOLD: CPU % to trigger scale-up
    SCALE_DOWN_THRESHOLD: CPU % to trigger scale-down
    SCALE_UP_COOLDOWN: Seconds to wait after scale-up
    SCALE_DOWN_COOLDOWN: Seconds to wait after scale-down
    NODE_READINESS_TIMEOUT: Max seconds to wait for node ready
    DRY_RUN: If true, simulate without making changes

    EC2 Configuration:
    SUBNET_ID: Subnet ID for launching worker instances
    SECURITY_GROUP_ID: Security group ID for worker instances
    IAM_INSTANCE_PROFILE: IAM instance profile name for workers
    AMI_ID: AMI ID for worker instances
    INSTANCE_TYPE: EC2 instance type for workers
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Config:
    """Autoscaler configuration from environment variables."""

    # Cluster configuration
    cluster_name: str
    prometheus_url: str
    k3s_master_url: Optional[str]  # Optional if not using drain

    # DynamoDB tables
    state_table_name: str
    wal_table_name: str

    # Scaling limits
    min_nodes: int
    max_nodes: int

    # Scaling thresholds
    scale_up_threshold: float  # CPU %
    scale_down_threshold: float  # CPU %

    # Cooldown periods (seconds)
    scale_up_cooldown: int
    scale_down_cooldown: int

    # Timeouts
    node_readiness_timeout: int

    # Operation mode
    dry_run: bool

    # EC2 configuration for worker instances
    subnet_id: str
    security_group_id: str
    iam_instance_profile: str
    ami_id: str
    instance_type: str

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        return cls(
            cluster_name=os.getenv("CLUSTER_NAME", "production-k3s"),
            prometheus_url=os.getenv(
                "PROMETHEUS_URL",
                "http://localhost:30900"
            ),
            k3s_master_url=os.getenv("K3S_MASTER_URL"),
            state_table_name=os.getenv(
                "STATE_TABLE_NAME",
                "k3s-cluster-state"
            ),
            wal_table_name=os.getenv(
                "WAL_TABLE_NAME",
                "k3s-scaling-wal"
            ),
            min_nodes=int(os.getenv("MIN_NODES", "2")),
            max_nodes=int(os.getenv("MAX_NODES", "10")),
            scale_up_threshold=float(os.getenv("SCALE_UP_THRESHOLD", "70")),
            scale_down_threshold=float(os.getenv("SCALE_DOWN_THRESHOLD", "30")),
            scale_up_cooldown=int(os.getenv("SCALE_UP_COOLDOWN", "300")),
            scale_down_cooldown=int(os.getenv("SCALE_DOWN_COOLDOWN", "900")),
            node_readiness_timeout=int(os.getenv("NODE_READINESS_TIMEOUT", "300")),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            subnet_id=os.getenv("SUBNET_ID", ""),
            security_group_id=os.getenv("SECURITY_GROUP_ID", ""),
            iam_instance_profile=os.getenv("IAM_INSTANCE_PROFILE", ""),
            ami_id=os.getenv("AMI_ID", ""),
            instance_type=os.getenv("INSTANCE_TYPE", "t3.small"),
        )


# Global config instance (lazy loaded)
_config: Optional[Config] = None


def get_config() -> Config:
    """Get or create the global configuration instance."""
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def reset_config() -> None:
    """Reset the global config (useful for testing)."""
    global _config
    _config = None
