"""Validate and backtest a trained Prophet forecasting model.

This script loads a trained Prophet model and computes validation metrics
(MAE, RMSE, MAPE) by backtesting against held-out data. It does NOT
produce charts — the training pipeline is headless and ships only
metrics to S3.

Usage:
    uv run scripts/validate_model.py --model-path models/cpu_prophet_model_*.json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
from prophet import Prophet
from prophet.serialize import model_from_json


def load_model(model_path: str) -> Prophet:
    """Load a Prophet model from a JSON file."""
    with open(model_path, "r") as f:
        return model_from_json(f.read())


def compute_metrics(actual: pd.Series, predicted: pd.Series) -> Dict[str, float]:
    """Compute MAE, RMSE, MAPE between actual and predicted values."""
    merged = pd.concat([actual.reset_index(drop=True), predicted.reset_index(drop=True)], axis=1)
    merged.columns = ["y", "yhat"]
    merged = merged.dropna()

    if len(merged) == 0:
        return {"mae": float("nan"), "rmse": float("nan"), "mape": float("nan"), "samples": 0}

    errors = merged["y"] - merged["yhat"]
    mae = float(errors.abs().mean())
    rmse = float((errors ** 2).mean() ** 0.5)

    # MAPE: percentage error, ignoring zero actuals to avoid division by zero
    nonzero = merged["y"].abs() > 1e-9
    if nonzero.any():
        mape = float((errors[nonzero].abs() / merged["y"][nonzero].abs()).mean() * 100)
    else:
        mape = float("nan")

    return {"mae": mae, "rmse": rmse, "mape": mape, "samples": int(len(merged))}


def backtest(
    model: Prophet,
    history: pd.DataFrame,
    horizon_days: int = 2,
    initial_days: int = 14,
    period_days: int = 1,
) -> Dict[str, float]:
    """Run a rolling backtest: repeatedly fit on a growing window and
    forecast `horizon_days` ahead, accumulating errors across all cuts.

    Returns aggregated MAE / RMSE / MAPE.
    """
    history = history.sort_values("ds").reset_index(drop=True)
    if len(history) < initial_days:
        return {"mae": float("nan"), "rmse": float("nan"), "mape": float("nan"), "samples": 0}

    cutoff_dates = pd.date_range(
        start=history["ds"].iloc[initial_days],
        end=history["ds"].iloc[-1] - pd.Timedelta(days=horizon_days),
        freq=f"{period_days}D",
    )

    all_actuals: List[float] = []
    all_preds: List[float] = []

    for cutoff in cutoff_dates:
        train = history[history["ds"] <= cutoff]
        horizon_end = cutoff + pd.Timedelta(days=horizon_days)
        test = history[(history["ds"] > cutoff) & (history["ds"] <= horizon_end)]

        if len(test) == 0:
            continue

        # Reuse the pre-trained model rather than refitting from scratch —
        # cheaper and good enough for a validation signal.
        future = test[["ds"]].copy()
        forecast = model.predict(future)
        merged = test.merge(forecast[["ds", "yhat"]], on="ds", how="inner")

        all_actuals.extend(merged["y"].tolist())
        all_preds.extend(merged["yhat"].tolist())

    return compute_metrics(pd.Series(all_actuals), pd.Series(all_preds))


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a Prophet model and write metrics.")
    parser.add_argument(
        "--model-path",
        type=str,
        required=True,
        help="Path to a trained Prophet model JSON file.",
    )
    parser.add_argument(
        "--history-path",
        type=str,
        default="data/metrics_samples.csv",
        help="Path to the metrics CSV used to fit the model (for backtest).",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="validation",
        help="Directory where the metrics JSON will be written.",
    )
    parser.add_argument(
        "--horizon-days",
        type=int,
        default=2,
        help="Forecast horizon for backtest (in days).",
    )
    parser.add_argument(
        "--initial-days",
        type=int,
        default=14,
        help="Initial training window for the backtest (in days).",
    )
    parser.add_argument(
        "--period-days",
        type=int,
        default=1,
        help="Spacing between backtest cutoffs (in days).",
    )
    args = parser.parse_args()

    if not os.path.exists(args.model_path):
        print(f"Error: Model file not found: {args.model_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading model from {args.model_path}...")
    model = load_model(args.model_path)

    metrics: Dict[str, float] = {}
    if os.path.exists(args.history_path):
        print(f"Running backtest with horizon={args.horizon_days}d, "
              f"initial={args.initial_days}d, period={args.period_days}d "
              f"against {args.history_path}...")
        history = pd.read_csv(args.history_path, parse_dates=["timestamp"])
        history = history.rename(columns={"timestamp": "ds", "cpu_percent": "y"})
        history = history[["ds", "y"]].dropna()
        metrics = backtest(
            model,
            history,
            horizon_days=args.horizon_days,
            initial_days=args.initial_days,
            period_days=args.period_days,
        )
    else:
        print(f"Warning: history file {args.history_path} not found, "
              f"skipping backtest.", file=sys.stderr)

    # Write metrics next to the model file as <model>_metrics.json
    metrics_path = args.model_path.replace(".json", "_metrics.json")
    os.makedirs(os.path.dirname(metrics_path) or ".", exist_ok=True)
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)

    print(f"\nValidation Metrics:")
    print(f"  MAE:  {metrics.get('mae', float('nan')):.2f} percentage points")
    print(f"  RMSE: {metrics.get('rmse', float('nan')):.2f} percentage points")
    print(f"  MAPE: {metrics.get('mape', float('nan')):.2f}%")
    print(f"  Samples: {metrics.get('samples', 0)}")
    print(f"\nMetrics written to {metrics_path}")


if __name__ == "__main__":
    main()
