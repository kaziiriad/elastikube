"""Flash sale detection module.

This module provides emergency response for sudden traffic spikes
by detecting rapid CPU increases over short time windows.

Layer 3 of the layered scaling architecture.

Detection is stateless: the CPU history comes from Prometheus (a
min_over_time subquery over the detection window, populated into
ClusterMetrics.worker_cpu_window_min), not from Lambda memory, which is
reset on every invocation.
"""

import logging
from typing import Optional

from utils.config import get_config

logger = logging.getLogger(__name__)


class FlashSaleDetector:
    """Detects flash sales based on rapid CPU increases.

    A flash sale is detected when current worker CPU exceeds the lowest
    worker CPU seen within the detection window by at least the configured
    threshold.

    Example: If CPU was 40% at its lowest in the last 2 minutes and is now
    75% (threshold 30%), a flash sale is detected and immediate scaling is
    triggered.

    Environment Variables (from config):
        FLASH_SALE_DETECTION_ENABLED: Enable/disable detection
        FLASH_SALE_CPU_SPIKE_THRESHOLD: CPU % increase to trigger (default: 30)
        FLASH_SALE_DETECTION_WINDOW_SECONDS: Time window to check (default: 120)
    """

    def __init__(self):
        """Initialize the flash sale detector."""
        self._config = get_config()

    def detect(self, current_cpu: float, window_min_cpu: Optional[float]) -> bool:
        """Check if a flash sale is occurring.

        Args:
            current_cpu: Current worker CPU usage percentage
            window_min_cpu: Lowest worker CPU within the detection window,
                or None if the history is unavailable

        Returns:
            True if flash sale detected, False otherwise
        """
        if not self._config.flash_sale_detection_enabled:
            return False

        if window_min_cpu is None:
            logger.debug("Flash sale: no CPU history for the detection window")
            return False

        cpu_increase = current_cpu - window_min_cpu
        threshold = self._config.flash_sale_cpu_spike_threshold

        if cpu_increase >= threshold:
            logger.warning(
                f"FLASH SALE DETECTED: CPU increased by {cpu_increase:.1f}% "
                f"({window_min_cpu:.1f}% -> {current_cpu:.1f}%) within "
                f"{self._config.flash_sale_detection_window_seconds}s "
                f"(threshold: {threshold}%)"
            )
            return True

        logger.debug(
            f"Flash sale: no spike (increase {cpu_increase:.1f}% < threshold {threshold}%)"
        )
        return False
