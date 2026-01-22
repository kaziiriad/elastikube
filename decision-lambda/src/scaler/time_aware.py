"""Time-aware scaling configuration.

This module provides time period detection for applying different
scaling thresholds based on time of day.

Layer 2 of the layered scaling architecture.
"""

import logging
from datetime import datetime, timezone, time
from typing import Optional, Tuple

from utils.config import get_config, Config

logger = logging.getLogger(__name__)


def get_time_period(now: datetime) -> Optional[str]:
    """Get the time period name for a given time.

    Args:
        now: Datetime to check (should be timezone-aware)

    Returns:
        Period name ("peak" or "off-peak") or None if time-aware disabled
    """
    config = get_config()

    if not config.time_aware_scaling_enabled:
        return None

    # Ensure we're working with UTC
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    try:
        peak_start = time.fromisoformat(config.peak_hour_start)
        peak_end = time.fromisoformat(config.peak_hour_end)
    except ValueError as e:
        logger.error(f"Invalid peak hour time format: {e}")
        return None

    current_time = now.time()

    # Handle period that doesn't cross midnight
    if peak_start <= peak_end:
        is_peak = peak_start <= current_time <= peak_end
    # Handle period that crosses midnight (e.g., 22:00 to 06:00)
    else:
        is_peak = current_time >= peak_start or current_time <= peak_end

    period = "peak" if is_peak else "off-peak"
    logger.debug(f"Time {now.isoformat()} is in period '{period}'")
    return period


def get_thresholds_for_period(period: Optional[str]) -> Tuple[float, float]:
    """Get scaling thresholds for a time period.

    Args:
        period: Period name from get_time_period() ("peak", "off-peak", or None)

    Returns:
        Tuple of (scale_up_threshold, scale_down_threshold)

    Note:
        If period is None or time-aware scaling is disabled, returns
        the default thresholds from config.
    """
    config = get_config()

    if period is None or not config.time_aware_scaling_enabled:
        # Use default thresholds
        return config.scale_up_threshold, config.scale_down_threshold

    if period == "peak":
        return config.peak_scale_up_threshold, config.peak_scale_down_threshold
    elif period == "off-peak":
        return config.off_peak_scale_up_threshold, config.off_peak_scale_down_threshold
    else:
        logger.warning(f"Unknown period '{period}', using default thresholds")
        return config.scale_up_threshold, config.scale_down_threshold
