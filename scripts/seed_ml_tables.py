"""Seed the ML training DynamoDB tables with synthetic data.

Dev-only utility for populating ``k3s-scaling-metrics-samples`` and
``k3s-scaling-history`` so the ml-training-cronjob Ansible role can
be tested end-to-end without waiting for the autoscaler to produce
real data.

Reuses the same ``generate_mock_data.py`` / ``generate_mock_scaling_history``
helpers that drive the offline EDA in ``ml_training/notebooks/`` so the
seeded rows have the same seasonality / daily-cycle structure the
real autoscaler produces.

Tables written
--------------
- ``k3s-scaling-metrics-samples`` — one row per (timestamp, sample_id)
  with cpu_percent, memory_percent, pending_pods, worker_count, etc.
  Schema matches what ``extract_data.py`` expects.
- ``k3s-scaling-history`` — one row per scaling decision.

Usage
-----
::

    # Default: 30 days, profile=poridhi-aws, region=ap-southeast-1
    AWS_PROFILE=poridhi-aws python scripts/seed_ml_tables.py

    # Custom window / profile / region / table names
    AWS_PROFILE=dev python scripts/seed_ml_tables.py \\
        --days 7 --region us-east-1 \\
        --metrics-table k3s-scaling-metrics-samples \\
        --history-table k3s-scaling-history

Caveats
-------
- **Dev / test only.** This script *writes* synthetic data into tables
  that the production autoscaler also writes to. Do not run against a
  real cluster's tables without first clearing them.
- Uses ``boto3.resource('dynamodb').Table.batch_writer`` (the same
  write path the autoscaler uses) and casts numpy floats to ``Decimal``
  because boto3 rejects raw ``float``.
- Timestamps are written at second precision (pandas 2.x rejects
  microsecond ISO timestamps in some code paths).
- Tables must already exist with the key schemas defined in
  ``infrastructure/pulumi/__main__.py``. This script does not create
  them.
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pandas as pd

# Re-use the repo's generator (avoids re-implementing seasonality logic).
# Resolve relative to this file so the script works no matter what cwd
# the caller invokes it from.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ML_TRAINING_SCRIPTS = _REPO_ROOT / "ml_training" / "scripts"
sys.path.insert(0, str(_ML_TRAINING_SCRIPTS))

from generate_mock_data import generate_mock_data, generate_mock_scaling_history  # noqa: E402


def _normalize(row: dict) -> dict:
    """Cast numpy types → native Python so boto3.lowlevel accepts them.

    Floats must become ``Decimal`` — boto3's high-level resource refuses
    raw float values with ``TypeError: "Float types are not supported."``
    """
    out: dict = {}
    for k, v in row.items():
        if isinstance(v, pd.Timestamp):
            out[k] = v.isoformat()
        elif hasattr(v, "item"):
            try:
                py = v.item()
            except (ValueError, TypeError):
                out[k] = str(v)
                continue
            if isinstance(py, float):
                out[k] = Decimal(str(py))
            else:
                out[k] = py
        elif isinstance(v, float):
            out[k] = Decimal(str(v))
        else:
            out[k] = v
    return out


def seed_metrics(
    table_name: str,
    days: int,
    region: str,
    profile: str | None,
    seed: int = 42,
) -> int:
    """Write ``days * 1440`` rows into the metrics samples table."""
    session = boto3.Session(profile_name=profile, region_name=region)
    table = session.resource("dynamodb").Table(table_name)

    df = generate_mock_data(days=days, seed=seed)
    df["sample_id"] = "sample-" + df["timestamp"].astype(str)
    df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    n = 0
    with table.batch_writer() as batch:
        for _, row in df.iterrows():
            batch.put_item(Item=_normalize(row.to_dict()))
            n += 1
            if n % 500 == 0:
                print(f"  metrics: {n}/{len(df)}")
    return n


def seed_history(
    table_name: str,
    days: int,
    region: str,
    profile: str | None,
    seed: int = 42,
    scale_up_threshold: int = 70,
) -> int:
    """Generate and write synthetic scaling-decision rows."""
    session = boto3.Session(profile_name=profile, region_name=region)
    table = session.resource("dynamodb").Table(table_name)

    metrics_df = generate_mock_data(days=days, seed=seed)
    history_df = generate_mock_scaling_history(
        metrics_df, scale_up_threshold=scale_up_threshold,
    )

    n = 0
    with table.batch_writer() as batch:
        for _, row in history_df.iterrows():
            d = _normalize(row.to_dict())
            if isinstance(d.get("timestamp"), pd.Timestamp):
                d["timestamp"] = d["timestamp"].isoformat()
            batch.put_item(Item=d)
            n += 1
    return n


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed ML training DynamoDB tables with synthetic data (dev only).",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Number of days of synthetic data to generate (default: 30)",
    )
    parser.add_argument(
        "--region", type=str, default="ap-southeast-1",
        help="AWS region (default: ap-southeast-1)",
    )
    parser.add_argument(
        "--profile", type=str, default=None,
        help="AWS profile name (default: use $AWS_PROFILE / ambient credentials)",
    )
    parser.add_argument(
        "--metrics-table", type=str, default="k3s-scaling-metrics-samples",
        help="DynamoDB table for metrics samples",
    )
    parser.add_argument(
        "--history-table", type=str, default="k3s-scaling-history",
        help="DynamoDB table for scaling decisions",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for deterministic synthetic data",
    )
    parser.add_argument(
        "--scale-up-threshold", type=int, default=70,
        help="CPU threshold above which history generator emits a SCALE_UP",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    print("=" * 60)
    print("Seeding DynamoDB tables for ML pipeline test (DEV ONLY)")
    print("=" * 60)
    print(f"region={args.region}  profile={args.profile or '(ambient)'}  days={args.days}")
    print(f"tables: {args.metrics_table}, {args.history_table}")
    print(f"started={datetime.now(timezone.utc).isoformat()}")

    t0 = time.time()
    n_metrics = seed_metrics(
        table_name=args.metrics_table,
        days=args.days,
        region=args.region,
        profile=args.profile,
        seed=args.seed,
    )
    print(f"metrics samples: {n_metrics} rows in {time.time()-t0:.1f}s")

    t0 = time.time()
    n_hist = seed_history(
        table_name=args.history_table,
        days=args.days,
        region=args.region,
        profile=args.profile,
        seed=args.seed,
        scale_up_threshold=args.scale_up_threshold,
    )
    print(f"scaling history: {n_hist} rows in {time.time()-t0:.1f}s")

    print("=" * 60)
    print("Seed complete!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
