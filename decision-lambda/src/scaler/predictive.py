"""Predictive scaling using Prophet model.

Loads a trained Prophet model and predicts future CPU usage
for proactive scaling decisions.

Environment variables:
    PREDICTIVE_SCALING_ENABLED: Enable/disable predictive scaling (default: false)
    PROPHET_MODEL_S3_BUCKET: S3 bucket containing model JSON
    PROPHET_MODEL_S3_KEY: S3 key for model JSON (e.g., models/cpu_prophet_model.json)
    PREDICTION_HORIZON_MINUTES: Minutes ahead to predict (default: 15)
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Global model cache (loaded once on cold start)
_prophet_model = None
_model_metadata = None


class CPUPredictor:
    """CPU predictor using Prophet model."""

    def __init__(
        self,
        model_json: str,
        prediction_horizon_minutes: int = 15,
    ):
        """Initialize the CPU predictor.

        Args:
            model_json: Prophet model JSON string
            prediction_horizon_minutes: Minutes ahead to predict
        """
        try:
            from prophet import Prophet
            from prophet.serializers import model_from_json
        except ImportError:
            raise ImportError(
                "prophet package not found. Add 'prophet' to Lambda dependencies. "
                "This is expected in development - the model will be loaded in production."
            )

        self.model = model_from_json(model_json)
        self.prediction_horizon_minutes = prediction_horizon_minutes

        # Extract model metadata (trained timestamp, etc.)
        self.metadata = {
            "prediction_horizon_minutes": prediction_horizon_minutes,
        }

    def predict_future_cpu(self, current_timestamp: Optional[datetime] = None) -> dict:
        """Predict CPU usage at prediction horizon.

        Args:
            current_timestamp: Current timestamp (default: now)

        Returns:
            Dictionary with prediction results:
                - predicted_cpu: Predicted CPU percentage at horizon
                - predicted_cpu_lower: Lower bound of confidence interval
                - predicted_cpu_upper: Upper bound of confidence interval
                - horizon_minutes: Prediction horizon
                - prediction_timestamp: When the prediction is for
        """
        if current_timestamp is None:
            current_timestamp = datetime.now(timezone.utc)

        # Create future dataframe for prediction horizon
        # Prophet requires 'ds' column with datetime
        import pandas as pd

        future = self.model.make_future_dataframe(
            periods=self.prediction_horizon_minutes,
            freq='min',  # 1-minute intervals
            include_history=False,
        )

        # Make prediction
        forecast = self.model.predict(future)

        # Get prediction at the horizon (last row)
        # forecast columns: ds, yhat, yhat_lower, yhat_upper, ...
        horizon_row = forecast.iloc[-1]

        return {
            "predicted_cpu": max(0, min(100, float(horizon_row["yhat"]))),  # Clamp to 0-100
            "predicted_cpu_lower": max(0, min(100, float(horizon_row["yhat_lower"]))),
            "predicted_cpu_upper": max(0, min(100, float(horizon_row["yhat_upper"]))),
            "horizon_minutes": self.prediction_horizon_minutes,
            "prediction_timestamp": horizon_row["ds"].isoformat(),
            "current_timestamp": current_timestamp.isoformat(),
        }


def load_model_from_s3(
    s3_client,
    bucket: str,
    key: str,
    prediction_horizon_minutes: int = 15,
) -> CPUPredictor:
    """Load Prophet model from S3.

    Args:
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        key: S3 object key
        prediction_horizon_minutes: Prediction horizon

    Returns:
        CPUPredictor instance

    Raises:
        RuntimeError: If model cannot be loaded
    """
    logger.info(f"Loading Prophet model from s3://{bucket}/{key}")

    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        model_json = response["Body"].read().decode("utf-8")

        predictor = CPUPredictor(
            model_json=model_json,
            prediction_horizon_minutes=prediction_horizon_minutes,
        )

        logger.info("✓ Prophet model loaded successfully")
        return predictor

    except ClientError as e:
        raise RuntimeError(f"Failed to load model from S3: {e}")
    except Exception as e:
        raise RuntimeError(f"Failed to initialize Prophet model: {e}")


def get_predictor() -> Optional[CPUPredictor]:
    """Get the CPU predictor (lazy loaded and cached).

    Loads the model from S3 on first call and caches it globally.
    Returns None if predictive scaling is disabled or model fails to load.

    Returns:
        CPUPredictor instance or None
    """
    global _prophet_model, _model_metadata

    # Check if predictive scaling is enabled
    enabled = os.environ.get("PREDICTIVE_SCALING_ENABLED", "false").lower() in ("true", "1", "yes")
    if not enabled:
        logger.debug("Predictive scaling is disabled")
        return None

    # Return cached model if already loaded
    if _prophet_model is not None:
        return _prophet_model

    # Load model from S3
    try:
        bucket = os.environ.get("PROPHET_MODEL_S3_BUCKET")
        key = os.environ.get("PROPHET_MODEL_S3_KEY")

        if not bucket or not key:
            logger.warning(
                "Predictive scaling enabled but PROPHET_MODEL_S3_BUCKET or "
                "PROPHET_MODEL_S3_KEY not set. Disabling predictive scaling."
            )
            return None

        s3_client = boto3.client("s3")
        horizon_minutes = int(os.environ.get("PREDICTION_HORIZON_MINUTES", "15"))

        _prophet_model = load_model_from_s3(
            s3_client=s3_client,
            bucket=bucket,
            key=key,
            prediction_horizon_minutes=horizon_minutes,
        )

        # Store metadata
        _model_metadata = {
            "s3_location": f"s3://{bucket}/{key}",
            "horizon_minutes": horizon_minutes,
            "loaded_at": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(f"Predictive scaling enabled (horizon: {horizon_minutes} minutes)")
        return _prophet_model

    except Exception as e:
        logger.error(f"Failed to load Prophet model: {e}")
        logger.warning("Predictive scaling disabled due to model load failure")
        logger.warning("Autoscaler will use reactive scaling (current CPU)")
        return None


def get_cpu_prediction(
    current_timestamp: Optional[datetime] = None,
) -> Optional[dict]:
    """Get CPU prediction with error handling.

    Wraps get_predictor().predict_future_cpu() with error handling.
    Returns None if predictive scaling is disabled or prediction fails.

    Args:
        current_timestamp: Current timestamp for prediction

    Returns:
        Prediction dict or None (see CPUPredictor.predict_future_cpu())
    """
    try:
        predictor = get_predictor()
        if predictor is None:
            return None

        prediction = predictor.predict_future_cpu(current_timestamp)

        logger.info(
            f"CPU Prediction: {prediction['predicted_cpu']:.1f}% "
            f"(range: {prediction['predicted_cpu_lower']:.1f}% - "
            f"{prediction['predicted_cpu_upper']:.1f}%) at "
            f"+{prediction['horizon_minutes']} minutes"
        )

        return prediction

    except Exception as e:
        logger.error(f"CPU prediction failed: {e}")
        return None


def is_prediction_confident(prediction: dict, threshold: float = 80.0) -> bool:
    """Check if prediction is within confidence threshold.

    Uses the confidence interval width to determine prediction confidence.
    Narrower intervals = higher confidence.

    Args:
        prediction: Prediction dict from get_cpu_prediction()
        threshold: Maximum allowed interval width (percentage points)

    Returns:
        True if prediction is confident (narrow interval), False otherwise
    """
    if prediction is None:
        return False

    interval_width = prediction["predicted_cpu_upper"] - prediction["predicted_cpu_lower"]
    return interval_width <= threshold
