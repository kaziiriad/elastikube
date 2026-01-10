"""Scaling decision engine.

Determines when to scale up or down based on:
- CPU usage
- Memory usage
- Pending pods
- Cooldown periods
- Min/max node limits
"""

from dataclasses import dataclass
from enum import Enum

from metrics.prometheus import ClusterMetrics
from state.cluster_state import ClusterState
from utils.config import get_config


class ScalingAction(str, Enum):
    """Possible scaling actions."""

    SCALE_UP = "SCALE_UP"
    SCALE_DOWN = "SCALE_DOWN"
    NO_OP = "NO_OP"


@dataclass
class ScalingDecision:
    """Decision made by the scaling engine."""

    action: ScalingAction
    reason: str
    current_nodes: int
    target_nodes: int
    cpu_percent: float
    memory_percent: float
    pending_pods: int

    def should_scale(self) -> bool:
        """Returns True if scaling action is needed."""
        return self.action in (ScalingAction.SCALE_UP, ScalingAction.SCALE_DOWN)

    def to_dict(self) -> dict:
        """Convert to dict for logging."""
        return {
            "action": self.action.value,
            "reason": self.reason,
            "current_nodes": self.current_nodes,
            "target_nodes": self.target_nodes,
            "cpu_percent": self.cpu_percent,
            "memory_percent": self.memory_percent,
            "pending_pods": self.pending_pods,
        }


class ScalingEngine:
    """Evaluates metrics and makes scaling decisions."""

    def __init__(self):
        """Initialize the scaling engine."""
        self._config = get_config()

    def evaluate(
        self,
        metrics: ClusterMetrics,
        state: ClusterState,
    ) -> ScalingDecision:
        """Evaluate metrics and determine scaling action.

        Scaling Logic:
        - Scale Up if: (CPU > threshold OR pending_pods >= 1) AND not at max AND not in cooldown
        - Scale Down if: (CPU < threshold AND Memory < threshold) AND not at min AND not in cooldown
        - Otherwise: No operation

        Args:
            metrics: Current cluster metrics
            state: Current cluster state

        Returns:
            Scaling decision with action and reasoning
        """
        current_nodes = metrics.total_nodes

        # Check cooldown periods
        if state.is_in_cooldown(self._config.scale_up_cooldown):
            return ScalingDecision(
                action=ScalingAction.NO_OP,
                reason=f"In scale-up cooldown ({self._config.scale_up_cooldown}s)",
                current_nodes=current_nodes,
                target_nodes=current_nodes,
                cpu_percent=metrics.worker_cpu_percent_avg,
                memory_percent=metrics.worker_memory_percent_avg,
                pending_pods=metrics.pending_pods,
            )

        if state.is_in_cooldown(self._config.scale_down_cooldown):
            return ScalingDecision(
                action=ScalingAction.NO_OP,
                reason=f"In scale-down cooldown ({self._config.scale_down_cooldown}s)",
                current_nodes=current_nodes,
                target_nodes=current_nodes,
                cpu_percent=metrics.worker_cpu_percent_avg,
                memory_percent=metrics.worker_memory_percent_avg,
                pending_pods=metrics.pending_pods,
            )

        # Check scale-up conditions (using worker metrics only)
        cpu_trigger = metrics.worker_cpu_percent_avg >= self._config.scale_up_threshold
        pods_trigger = metrics.pending_pods >= 1

        if (cpu_trigger or pods_trigger) and current_nodes < self._config.max_nodes:
            # Build reason string based on actual trigger
            if pods_trigger:
                reason = f"Pending pods ({metrics.pending_pods}) >= 1"
            else:
                reason = f"Worker CPU ({metrics.worker_cpu_percent_avg:.1f}%) >= threshold ({self._config.scale_up_threshold}%)"

            return ScalingDecision(
                action=ScalingAction.SCALE_UP,
                reason=reason,
                current_nodes=current_nodes,
                target_nodes=current_nodes + 1,
                cpu_percent=metrics.worker_cpu_percent_avg,
                memory_percent=metrics.worker_memory_percent_avg,
                pending_pods=metrics.pending_pods,
            )

        # Check scale-down conditions (using worker metrics only)
        cpu_low = metrics.worker_cpu_percent_avg < self._config.scale_down_threshold
        memory_low = metrics.worker_memory_percent_avg < 50  # Memory threshold for scale-down

        if cpu_low and memory_low and current_nodes > self._config.min_nodes:
            return ScalingDecision(
                action=ScalingAction.SCALE_DOWN,
                reason=(
                    f"Worker CPU ({metrics.worker_cpu_percent_avg:.1f}%) < threshold "
                    f"({self._config.scale_down_threshold}%) "
                    f"AND memory ({metrics.worker_memory_percent_avg:.1f}%) < 50%"
                ),
                current_nodes=current_nodes,
                target_nodes=current_nodes - 1,
                cpu_percent=metrics.worker_cpu_percent_avg,
                memory_percent=metrics.worker_memory_percent_avg,
                pending_pods=metrics.pending_pods,
            )

        # No scaling needed
        if current_nodes >= self._config.max_nodes:
            return ScalingDecision(
                action=ScalingAction.NO_OP,
                reason=f"At maximum node count ({self._config.max_nodes})",
                current_nodes=current_nodes,
                target_nodes=current_nodes,
                cpu_percent=metrics.worker_cpu_percent_avg,
                memory_percent=metrics.worker_memory_percent_avg,
                pending_pods=metrics.pending_pods,
            )

        if current_nodes <= self._config.min_nodes:
            return ScalingDecision(
                action=ScalingAction.NO_OP,
                reason=f"At minimum node count ({self._config.min_nodes})",
                current_nodes=current_nodes,
                target_nodes=current_nodes,
                cpu_percent=metrics.worker_cpu_percent_avg,
                memory_percent=metrics.worker_memory_percent_avg,
                pending_pods=metrics.pending_pods,
            )

        return ScalingDecision(
            action=ScalingAction.NO_OP,
            reason="Within normal operating parameters",
            current_nodes=current_nodes,
            target_nodes=current_nodes,
            cpu_percent=metrics.worker_cpu_percent_avg,
            memory_percent=metrics.worker_memory_percent_avg,
            pending_pods=metrics.pending_pods,
        )
