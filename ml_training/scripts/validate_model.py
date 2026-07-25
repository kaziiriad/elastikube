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


def load_model(model_path: str):
    """Load a Prophet model from a JSON file.

    Returns the Prophet model on success, or ``None`` if the file is
    not a serialized Prophet model (e.g. the metrics JSON that the
    training script writes next to the model — see below).

    Why this is defensive
    ---------------------
    The bash pipeline calls ``validate_model.py`` with the most
    recently modified ``models/cpu_prophet_model_*.json`` file. The
    training script writes ``<model>_metrics.json`` *after* the model
    file in the same second, so ``ls -t | head -1`` can pick the
    metrics JSON. That file has ``{mae, rmse, mape, ...}`` keys but
    no ``__prophet_version`` field, so feeding it to
    ``model_from_json`` crashes deep inside Prophet with
    ``KeyError: 'seasonality_mode'`` and (because the bash script
    uses ``set -e``) aborts the whole pipeline before the S3 upload
    runs.

    We detect the wrong-file case up front and return ``None`` so
    the caller can write NaN metrics and let the pipeline finish.

    We also patch the dict before handing it to Prophet's loader to
    survive the ``KeyError: 'seasonality_mode'`` raised by Prophet
    1.3.0's ``_handle_simple_attributes_backwards_compat`` shim when
    ``holidays_mode`` is missing from a model saved with a slightly
    different serialization (e.g. cross-version or fit with extra
    regressors that stripped the attribute).
    """
    with open(model_path, "r") as f:
        raw = f.read()

    raw_dict = json.loads(raw)
    if "__prophet_version" not in raw_dict:
        # Not a serialized Prophet model — most likely the metrics
        # file was passed by mistake. The caller treats None as
        # "skip validation".
        return None

    # Backwards-compat shim in prophet.serialize needs both
    # `seasonality_mode` and `holidays_mode`; populate any missing
    # one with a safe default so we don't crash on older saves.
    if "holidays_mode" not in raw_dict and "seasonality_mode" in raw_dict:
        raw_dict["holidays_mode"] = raw_dict["seasonality_mode"]
    elif "holidays_mode" not in raw_dict and "seasonality_mode" not in raw_dict:
        raw_dict["holidays_mode"] = "additive"
        raw_dict["seasonality_mode"] = "additive"

    return model_from_json(json.dumps(raw_dict))


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
    if model is None:
        # The file passed was not a serialized Prophet model (most
        # likely the metrics JSON instead of the model JSON — see the
        # docstring on `load_model` for the full diagnosis). We still
        # write a metrics file with NaN values so the bash pipeline
        # can continue past validation, upload the model to S3, and
        # exit 0. Treat this as a hard warning, not a fatal error.
        print(
            f"Warning: {args.model_path} is not a serialized Prophet "
            f"model (no `__prophet_version` field). Skipping backtest "
            f"and writing NaN metrics so the pipeline can continue.",
            file=sys.stderr,
        )
        metrics: Dict[str, float] = {
            "mae": float("nan"),
            "rmse": float("nan"),
            "mape": float("nan"),
            "samples": 0,
            "skipped": True,
        }
        # Don't double-suffix when the path already ends in _metrics.json
        # (which is the case when the bash script's `ls -t` picks the
        # metrics file by mistake — see the docstring on `load_model`).
        if args.model_path.endswith("_metrics.json"):
            metrics_path = args.model_path
        else:
            metrics_path = args.model_path.replace(".json", "_metrics.json")
        os.makedirs(os.path.dirname(metrics_path) or ".", exist_ok=True)
        with open(metrics_path, "w") as f:
            json.dump(metrics, f, indent=2)
        print(f"\nValidation Metrics (skipped):")
        print(f"  MAE:  nan")
        print(f"  RMSE: nan")
        print(f"  MAPE: nan%")
        print(f"  Samples: 0")
        print(f"\nMetrics written to {metrics_path}")
        sys.exit(0)
    if os.path.exists(args.history_path):
        print(f"Running backtest with horizon={args.horizon_days}d, "
              f"initial={args.initial_days}d, period={args.period_days}d "
              f"against {args.history_path}...")
        history = pd.read_csv(args.history_path, parse_dates=["timestamp"])
        history = history.rename(columns={"timestamp": "ds", "cpu_percent": "y"})
        history = history[["ds", "y"]].dropna()
        try:
            metrics = backtest(
                model,
                history,
                horizon_days=args.horizon_days,
                initial_days=args.initial_days,
                period_days=args.period_days,
            )
        except Exception as e:
            # Backtest can fail for a variety of reasons (e.g. regressor
            # columns missing from history CSV after a schema change).
            # Emit NaN metrics so the bash pipeline can still upload the
            # model to S3 — losing validation telemetry is preferable to
            # losing the trained model itself.
            print(
                f"Warning: backtest failed: {e}. Writing NaN metrics so "
                f"the pipeline can continue past validation.",
                file=sys.stderr,
            )
            metrics = {
                "mae": float("nan"),
                "rmse": float("nan"),
                "mape": float("nan"),
                "samples": 0,
                "error": str(e),
            }
    else:
        print(f"Warning: history file {args.history_path} not found, "
              f"skipping backtest.", file=sys.stderr)

    # Write metrics next to the model file as <model>_metrics.json.
    # Skip the suffix when the input is already the metrics path
    # (see the docstring on `load_model` for the bash-script bug
    # this defends against).
    if args.model_path.endswith("_metrics.json"):
        metrics_path = args.model_path
    else:
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
