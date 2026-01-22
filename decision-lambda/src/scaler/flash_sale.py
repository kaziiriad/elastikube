"""Flash sale detection module.

This module provides emergency response for sudden traffic spikes
by detecting rapid CPU increases over short time windows.

Layer 3 of the layered scaling architecture.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from utils.config import get_config

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class CPUSample:
    """A CPU usage sample for flash sale detection.

    Attributes:
        timestamp: When the sample was taken (UTC)
        cpu_percent: CPU usage percentage
    """

    timestamp: datetime
    cpu_percent: float


@dataclass
class FlashSaleState:
    """State for flash sale detection.

    Stores recent CPU samples to detect spikes. This state is persisted
    in DynamoDB to survive Lambda restarts.

    Attributes:
        samples: List of recent CPU samples (max 24 for 2-minute window at 5s intervals)
        last_detection_time: When flash sale was last detected
        active_flash_sale: Whether a flash sale is currently active
    """

    samples: List[CPUSample] = field(default_factory=list)
    last_detection_time: Optional[datetime] = None
    active_flash_sale: bool = False

    def add_sample(self, cpu_percent: float, max_samples: int = 24):
        """Add a new CPU sample, removing old samples.

        Args:
            cpu_percent: Current CPU usage
            max_samples: Maximum samples to keep (default 24 for 2-min window)
        """
        self.samples.append(CPUSample(
            timestamp=datetime.now(timezone.utc),
            cpu_percent=cpu_percent,
        ))

        # Keep only recent samples (max_samples)
        if len(self.samples) > max_samples:
            self.samples = self.samples[-max_samples:]

    def get_samples_in_window(self, window_seconds: int) -> List[CPUSample]:
        """Get samples within the time window.

        Args:
            window_seconds: Time window in seconds

        Returns:
            List of samples within the window
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        return [s for s in self.samples if s.timestamp > cutoff]


# =============================================================================
# Flash Sale Detector
# =============================================================================


class FlashSaleDetector:
    """Detects flash sales based on rapid CPU increases.

    A flash sale is detected when CPU usage increases by more than
    the configured threshold within the configured time window.

    Example: If CPU goes from 40% to 75% in 2 minutes (threshold 30%),
    a flash sale is detected and immediate scaling is triggered.

    Environment Variables (from config):
        FLASH_SALE_DETECTION_ENABLED: Enable/disable detection
        FLASH_SALE_CPU_SPIKE_THRESHOLD: CPU % increase to trigger (default: 30)
        FLASH_SALE_DETECTION_WINDOW_SECONDS: Time window to check (default: 120)
    """

    def __init__(self):
        """Initialize the flash sale detector."""
        self._config = get_config()
        self._state = FlashSaleState()

    def detect(self, current_cpu: float) -> bool:
        """Check if a flash sale is occurring.

        Args:
            current_cpu: Current CPU usage percentage

        Returns:
            True if flash sale detected, False otherwise
        """
        if not self._config.flash_sale_detection_enabled:
            return False

        # Add current sample
        self._state.add_sample(current_cpu)

        # Get samples in detection window
        samples = self._state.get_samples_in_window(
            self._config.flash_sale_detection_window_seconds
        )

        # Need at least 2 samples to detect a spike
        if len(samples) < 2:
            logger.debug(f"Flash sale: need more samples (have {len(samples)})")
            return False

        # Calculate CPU increase
        oldest_cpu = samples[0].cpu_percent
        cpu_increase = current_cpu - oldest_cpu

        # Check if spike exceeds threshold
        if cpu_increase >= self._config.flash_sale_cpu_spike_threshold:
            time_delta = (samples[-1].timestamp - samples[0].timestamp).total_seconds()
            logger.warning(
                f"FLASH SALE DETECTED: CPU increased by {cpu_increase:.1f}% "
                f"({oldest_cpu:.1f}% -> {current_cpu:.1f}%) in {time_delta:.0f}s "
                f"(threshold: {self._config.flash_sale_cpu_spike_threshold}%)"
            )
            self._state.last_detection_time = datetime.now(timezone.utc)
            self._state.active_flash_sale = True
            return True

        # Reset active flag if no spike
        if self._state.active_flash_sale:
            logger.debug("Flash sale: no longer active")
            self._state.active_flash_sale = False

        logger.debug(
            f"Flash sale: no spike (increase {cpu_increase:.1f}% "
            f"< threshold {self._config.flash_sale_cpu_spike_threshold}%)"
        )
        return False

    def is_active(self) -> bool:
        """Check if flash sale is currently active.

        Returns:
            True if a flash sale was recently detected
        """
        return self._state.active_flash_sale

    def reset(self):
        """Reset the detector state (e.g., after scaling completes)."""
        self._state = FlashSaleState()
        logger.debug("Flash sale detector state reset")
