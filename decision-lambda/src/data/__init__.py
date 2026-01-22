"""Data collection module for autoscaling history and metrics.

This module provides interfaces for recording scaling decisions and metrics
for ML model training and audit trail purposes.
"""

from data.recorder import DataRecorder, DynamoDBRecorder

__all__ = ["DataRecorder", "DynamoDBRecorder"]
