"""Configuration management for K3s autoscaler.

Environment variables:
    CLUSTER_NAME: Name of the K3s cluster
    PROMETHEUS_URL: Prometheus NodePort URL (http://<worker-ip>:30900)
    K3S_MASTER_URL: Kubernetes API URL for drain operations
    STATE_TABLE_NAME: DynamoDB table for cluster state
    WAL_TABLE_NAME: DynamoDB table for write-ahead log
    DECISION_TABLE_NAME: DynamoDB table for scaling history
    METRICS_TABLE_NAME: DynamoDB table for metrics samples
    DATA_RETENTION_DAYS: Days to retain historical data (default: 90)
    MIN_NODES: Minimum number of worker nodes
    MAX_NODES: Maximum number of worker nodes
    SCALE_UP_THRESHOLD: CPU % to trigger scale-up (used if time-aware disabled)
    SCALE_DOWN_THRESHOLD: CPU % to trigger scale-down (used if time-aware disabled)
    SCALE_UP_COOLDOWN: Seconds to wait after scale-up
    SCALE_DOWN_COOLDOWN: Seconds to wait after scale-down
    NODE_READINESS_TIMEOUT: Max seconds to wait for node ready
    DRY_RUN: If true, simulate without making changes

    Time-Aware Scaling (Layer 2):
    TIME_AWARE_SCALING_ENABLED: Enable time-aware thresholds (default: false)
    PEAK_HOUR_START: Peak hours start time in HH:MM format (default: 09:00)
    PEAK_HOUR_END: Peak hours end time in HH:MM format (default: 21:00)
    PEAK_SCALE_UP_THRESHOLD: CPU threshold during peak (default: 85)
    PEAK_SCALE_DOWN_THRESHOLD: CPU threshold during peak (default: 60)
    OFF_PEAK_SCALE_UP_THRESHOLD: CPU threshold off-peak (default: 60)
    OFF_PEAK_SCALE_DOWN_THRESHOLD: CPU threshold off-peak (default: 40)

    Flash Sale Detection (Layer 3):
    FLASH_SALE_DETECTION_ENABLED: Enable flash sale detection (default: false)
    FLASH_SALE_CPU_SPIKE_THRESHOLD: CPU % increase to detect flash sale (default: 30)
    FLASH_SALE_DETECTION_WINDOW_SECONDS: Time window to check for spike (default: 120)

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
    decision_table_name: str
    metrics_table_name: str
    data_retention_days: int

    # Scaling limits
    min_nodes: int
    max_nodes: int

    # Scaling thresholds (used when time-aware scaling is disabled)
    scale_up_threshold: float  # CPU %
    scale_down_threshold: float  # CPU %

    # Cooldown periods (seconds)
    scale_up_cooldown: int
    scale_down_cooldown: int

    # Timeouts
    node_readiness_timeout: int

    # Operation mode
    dry_run: bool

    # Layer 2: Time-Aware Scaling
    time_aware_scaling_enabled: bool
    peak_hour_start: str  # HH:MM format
    peak_hour_end: str  # HH:MM format
    peak_scale_up_threshold: float
    peak_scale_down_threshold: float
    off_peak_scale_up_threshold: float
    off_peak_scale_down_threshold: float

    # Layer 3: Flash Sale Detection
    flash_sale_detection_enabled: bool
    flash_sale_cpu_spike_threshold: float  # CPU % increase
    flash_sale_detection_window_seconds: int

    # EC2 configuration for worker instances
    subnet_id: str
    security_group_id: str
    iam_instance_profile: str
    ami_id: str
    instance_type: str

    # S3 configuration for worker bootstrap script
    user_data_s3_bucket: str  # S3 bucket containing bootstrap script
    user_data_s3_key: str     # S3 object key for bootstrap script

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        return cls(
            # Cluster configuration
            cluster_name=os.getenv("CLUSTER_NAME", "production-k3s"),
            prometheus_url=os.getenv(
                "PROMETHEUS_URL",
                "http://localhost:30900"
            ),
            k3s_master_url=os.getenv("K3S_MASTER_URL"),
            # DynamoDB tables
            state_table_name=os.getenv(
                "STATE_TABLE_NAME",
                "k3s-cluster-state"
            ),
            wal_table_name=os.getenv(
                "WAL_TABLE_NAME",
                "k3s-scaling-wal"
            ),
            decision_table_name=os.getenv(
                "DECISION_TABLE_NAME",
                "k3s-scaling-history"
            ),
            metrics_table_name=os.getenv(
                "METRICS_TABLE_NAME",
                "k3s-scaling-metrics-samples"
            ),
            data_retention_days=int(os.getenv("DATA_RETENTION_DAYS", "90")),
            # Scaling limits
            min_nodes=int(os.getenv("MIN_NODES", "2")),
            max_nodes=int(os.getenv("MAX_NODES", "10")),
            # Scaling thresholds (fallback when time-aware disabled)
            scale_up_threshold=float(os.getenv("SCALE_UP_THRESHOLD", "70")),
            scale_down_threshold=float(os.getenv("SCALE_DOWN_THRESHOLD", "30")),
            # Cooldown periods
            scale_up_cooldown=int(os.getenv("SCALE_UP_COOLDOWN", "300")),
            scale_down_cooldown=int(os.getenv("SCALE_DOWN_COOLDOWN", "900")),
            # Timeouts
            node_readiness_timeout=int(os.getenv("NODE_READINESS_TIMEOUT", "300")),
            # Operation mode
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            # Layer 2: Time-Aware Scaling
            time_aware_scaling_enabled=os.getenv("TIME_AWARE_SCALING_ENABLED", "false").lower() == "true",
            peak_hour_start=os.getenv("PEAK_HOUR_START", "09:00"),
            peak_hour_end=os.getenv("PEAK_HOUR_END", "21:00"),
            peak_scale_up_threshold=float(os.getenv("PEAK_SCALE_UP_THRESHOLD", "85")),
            peak_scale_down_threshold=float(os.getenv("PEAK_SCALE_DOWN_THRESHOLD", "60")),
            off_peak_scale_up_threshold=float(os.getenv("OFF_PEAK_SCALE_UP_THRESHOLD", "60")),
            off_peak_scale_down_threshold=float(os.getenv("OFF_PEAK_SCALE_DOWN_THRESHOLD", "40")),
            # Layer 3: Flash Sale Detection
            flash_sale_detection_enabled=os.getenv("FLASH_SALE_DETECTION_ENABLED", "false").lower() == "true",
            flash_sale_cpu_spike_threshold=float(os.getenv("FLASH_SALE_CPU_SPIKE_THRESHOLD", "30")),
            flash_sale_detection_window_seconds=int(os.getenv("FLASH_SALE_DETECTION_WINDOW_SECONDS", "120")),
            # EC2 configuration
            subnet_id=os.getenv("SUBNET_ID", ""),
            security_group_id=os.getenv("SECURITY_GROUP_ID", ""),
            iam_instance_profile=os.getenv("IAM_INSTANCE_PROFILE", ""),
            ami_id=os.getenv("AMI_ID", ""),
            instance_type=os.getenv("INSTANCE_TYPE", "t3.small"),
            # S3 configuration
            user_data_s3_bucket=os.getenv("USER_DATA_S3_BUCKET", "k3s-userdata-production-k3s"),
            user_data_s3_key=os.getenv("USER_DATA_S3_KEY", "user-data/worker-bootstrap.sh"),
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
