"""Tests for CPUPredictor forecast timing."""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

# Prophet (and numpy/pandas) aren't Lambda deps; skip when absent.
prophet = pytest.importorskip("prophet")
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from prophet.serialize import model_to_json  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from scaler.predictive import CPUPredictor  # noqa: E402

TRAINING_END = datetime(2026, 1, 1, 0, 0)  # naive UTC, like the training pipeline


@pytest.fixture(scope="module")
def model_json():
    ds = pd.date_range(end=TRAINING_END, periods=288, freq="10min")
    rng = np.random.default_rng(0)
    df = pd.DataFrame({
        "ds": ds,
        "y": 50 + 10 * np.sin(np.arange(len(ds)) / 24) + rng.normal(0, 1, len(ds)),
        "pending_pods": rng.integers(0, 5, len(ds)),
        "worker_count": rng.integers(2, 5, len(ds)),
    })
    model = prophet.Prophet(daily_seasonality=False, weekly_seasonality=False)
    model.add_regressor("pending_pods")
    model.add_regressor("worker_count")
    model.fit(df)
    return model_to_json(model)


REGRESSORS = {"pending_pods": 2, "worker_count": 3}


def test_prediction_is_horizon_after_current_time_not_training_end(model_json):
    predictor = CPUPredictor(model_json, prediction_horizon_minutes=15)
    now = datetime(2026, 1, 4, 12, 0, tzinfo=timezone.utc)

    result = predictor.predict_future_cpu(now, REGRESSORS)

    assert result["prediction_timestamp"] == (
        now.replace(tzinfo=None) + timedelta(minutes=15)
    ).isoformat()


def test_tz_aware_current_time_is_converted_to_utc(model_json):
    predictor = CPUPredictor(model_json, prediction_horizon_minutes=15)
    now_plus8 = datetime(2026, 1, 4, 20, 0, tzinfo=timezone(timedelta(hours=8)))

    result = predictor.predict_future_cpu(now_plus8, REGRESSORS)

    assert result["prediction_timestamp"] == "2026-01-04T12:15:00"


def test_prediction_clamped_to_percent_range(model_json):
    predictor = CPUPredictor(model_json, prediction_horizon_minutes=15)

    result = predictor.predict_future_cpu(
        datetime(2026, 1, 2, tzinfo=timezone.utc), REGRESSORS
    )

    for key in ("predicted_cpu", "predicted_cpu_lower", "predicted_cpu_upper"):
        assert 0 <= result[key] <= 100
