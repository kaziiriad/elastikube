"""Scaling decision engine.

Determines when to scale up or down based on:
- CPU usage (current and predicted)
- Memory usage
- Pending pods
- Cooldown periods
- Min/max node limits
- Time of day (Layer 2: Time-Aware Scaling)
- ML prediction (Layer 4: Predictive Scaling)
"""

import logging
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timezone

from metrics.prometheus import ClusterMetrics
from state.cluster_state import ClusterState
from utils.config import get_config
from scaler.time_aware import get_time_period, get_thresholds_for_period
from scaler.flash_sale import FlashSaleDetector

# Layer 4: Predictive Scaling (optional, lazy-loaded)
try:
    from scaler.predictive import get_cpu_prediction
    PREDICTIVE_AVAILABLE = True
except ImportError:
    PREDICTIVE_AVAILABLE = False

logger = logging.getLogger(__name__)


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
        self._flash_sale_detector = FlashSaleDetector()

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

        Time-Aware Scaling (Layer 2):
        When enabled, thresholds are adjusted based on time period (peak/off-peak).

        Args:
            metrics: Current cluster metrics
            state: Current cluster state

        Returns:
            Scaling decision with action and reasoning
        """
        current_nodes = metrics.worker_count  # Use worker count, not total (excludes master)

        # Layer 3: Flash Sale Detection (emergency response, overrides cooldown)
        # Check BEFORE cooldown to allow immediate scaling during spikes
        if self._flash_sale_detector.detect(metrics.worker_cpu_percent_avg):
            if current_nodes < self._config.max_nodes:
                logger.warning("FLASH SALE: Triggering immediate scale-up")
                return ScalingDecision(
                    action=ScalingAction.SCALE_UP,
                    reason=f"FLASH SALE: CPU spike detected ({metrics.worker_cpu_percent_avg:.1f}%)",
                    current_nodes=current_nodes,
                    target_nodes=current_nodes + 1,
                    cpu_percent=metrics.worker_cpu_percent_avg,
                    memory_percent=metrics.worker_memory_percent_avg,
                    pending_pods=metrics.pending_pods,
                )
            else:
                logger.warning("FLASH SALE: At max nodes, cannot scale further")

        # Layer 2: Get time-aware thresholds if enabled
        now = datetime.now(timezone.utc)
        period = get_time_period(now)
        scale_up_threshold, scale_down_threshold = get_thresholds_for_period(period)

        # Log time-aware context if enabled
        if period:
            logger.info(
                f"Time-aware scaling: period={period}, "
                f"scale_up_threshold={scale_up_threshold}%, "
                f"scale_down_threshold={scale_down_threshold}%"
            )

        # Layer 4: Predictive Scaling - Get CPU prediction
        # Use predicted CPU for scale-up decisions (proactive)
        # Use current CPU for scale-down decisions (conservative)
        predicted_cpu = None
        cpu_for_scale_up = metrics.worker_cpu_percent_avg  # Default to current

        if PREDICTIVE_AVAILABLE:
            # Pass current regressor values so predict_future_cpu can populate
            # them in the future dataframe (model was trained with these as
            # extra regressors; predict() rejects missing columns).
            prediction = get_cpu_prediction(
                current_timestamp=now,
                regressor_values={
                    "pending_pods": metrics.pending_pods,
                    "worker_count": metrics.worker_count,
                },
            )
            if prediction:
                predicted_cpu = prediction["predicted_cpu"]
                cpu_for_scale_up = predicted_cpu

                # Log prediction vs current
                cpu_diff = predicted_cpu - metrics.worker_cpu_percent_avg
                logger.info(
                    f"Layer 4 Predictive Scaling: Current CPU={metrics.worker_cpu_percent_avg:.1f}%, "
                    f"Predicted CPU (+{prediction['horizon_minutes']}min)={predicted_cpu:.1f}% "
                    f"(Δ{cpu_diff:+.1f}%)"
                )
            else:
                logger.debug("Predictive scaling not available, using current CPU")
        else:
            logger.debug("Predictive scaling module not available")

        # Check cooldown periods
        # Scale-up cooldown blocks ALL scale-up operations
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

        # Check scale-up conditions FIRST (before scale-down cooldown)
        # Pending pods should ALWAYS trigger scale-up, regardless of scale-down cooldown
        # Layer 4: Use predicted CPU for scale-up decisions (proactive scaling)
        cpu_trigger = cpu_for_scale_up >= scale_up_threshold
        pods_trigger = metrics.pending_pods >= 1

        if (cpu_trigger or pods_trigger) and current_nodes < self._config.max_nodes:
            # Build reason string based on actual trigger
            if pods_trigger:
                reason = f"Pending pods ({metrics.pending_pods}) >= 1"
            else:
                period_info = f" [{period}]" if period else ""
                # Indicate if prediction was used
                cpu_source = "Predicted CPU" if predicted_cpu is not None else "Worker CPU"
                reason = (
                    f"{cpu_source} ({cpu_for_scale_up:.1f}%) >= threshold "
                    f"({scale_up_threshold}%){period_info}"
                )
                if predicted_cpu is not None:
                    reason += f" [Current: {metrics.worker_cpu_percent_avg:.1f}%]"

            return ScalingDecision(
                action=ScalingAction.SCALE_UP,
                reason=reason,
                current_nodes=current_nodes,
                target_nodes=current_nodes + 1,
                cpu_percent=cpu_for_scale_up,  # Record the CPU value that triggered
                memory_percent=metrics.worker_memory_percent_avg,
                pending_pods=metrics.pending_pods,
            )

        # Check scale-down cooldown (only blocks scale-down, not scale-up)
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

        # Check scale-down conditions (using worker metrics only)
        cpu_low = metrics.worker_cpu_percent_avg < scale_down_threshold
        memory_low = metrics.worker_memory_percent_avg < 50  # Memory threshold for scale-down

        if cpu_low and memory_low and current_nodes > self._config.min_nodes:
            period_info = f" [{period}]" if period else ""
            return ScalingDecision(
                action=ScalingAction.SCALE_DOWN,
                reason=(
                    f"Worker CPU ({metrics.worker_cpu_percent_avg:.1f}%) < threshold "
                    f"({scale_down_threshold}%){period_info} "
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
