"""Generate mock metrics data for ML pipeline testing.

Creates synthetic time-series data that mimics real cluster patterns:
- Daily seasonality (peak 9AM-9PM, off-peak 9PM-9AM)
- Weekly seasonality (weekday vs weekend)
- Random noise and spikes
- Pending pod variations

Usage:
    python generate_mock_data.py --days 30 --output-dir data
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


# Match production configuration
PEAK_HOUR_START = 9  # 9 AM
PEAK_HOUR_END = 21    # 9 PM


def generate_mock_data(
    days: int = 30,
    sample_interval_minutes: int = 2,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate mock metrics data with realistic patterns.

    Args:
        days: Number of days of data to generate
        sample_interval_minutes: Interval between samples (default: 2 min)
        seed: Random seed for reproducibility

    Returns:
        DataFrame with columns: timestamp, cpu_percent, memory_percent,
            pending_pods, worker_count, ready_nodes, total_nodes
    """
    np.random.seed(seed)

    # Generate timestamps
    start_date = datetime.now(timezone.utc) - timedelta(days=days)

    # Calculate number of periods needed
    periods_per_day = (24 * 60) // sample_interval_minutes  # e.g., 720 for 2-min intervals
    total_periods = days * periods_per_day

    timestamps = pd.date_range(
        start=start_date,
        periods=total_periods,
        freq=f'{sample_interval_minutes}min',
        tz='UTC'
    )

    n_samples = len(timestamps)
    print(f"Generating {n_samples} samples ({days} days at {sample_interval_minutes}-min intervals)")

    # Extract time features
    df = pd.DataFrame({'timestamp': timestamps})
    df['hour'] = df['timestamp'].dt.hour
    df['day_of_week'] = df['timestamp'].dt.dayofweek
    df['is_weekend'] = df['day_of_week'] >= 5
    df['is_peak_hour'] = (df['hour'] >= PEAK_HOUR_START) & (df['hour'] < PEAK_HOUR_END)

    # Base CPU patterns
    # Weekend baseline is lower
    base_cpu = np.where(df['is_weekend'], 30, 50)

    # Add daily seasonality (sine wave pattern for day/night)
    # Peak at 3PM (hour 15), trough at 3AM (hour 3)
    daily_pattern = 20 * np.sin(2 * np.pi * (df['hour'] - 3) / 24)

    # Add peak hour boost
    peak_boost = np.where(df['is_peak_hour'], 15, 0)

    # Add random noise
    noise = np.random.normal(0, 5, n_samples)

    # Combine for CPU
    df['cpu_percent'] = base_cpu + daily_pattern + peak_boost + noise

    # Add occasional spikes (flash sales)
    spike_indices = np.random.choice(n_samples, size=int(n_samples * 0.02), replace=False)
    spike_duration_samples = 5  # 10 minutes worth of spikes
    for spike_idx in spike_indices:
        end_idx = min(spike_idx + spike_duration_samples, n_samples)
        df.loc[spike_idx:end_idx, 'cpu_percent'] += np.random.uniform(20, 40)

    # Clip CPU to valid range
    df['cpu_percent'] = df['cpu_percent'].clip(5, 95)

    # Memory correlates with CPU but with less variation
    df['memory_percent'] = df['cpu_percent'] * 0.6 + np.random.normal(0, 3, n_samples)
    df['memory_percent'] = df['memory_percent'].clip(20, 90)

    # Pending pods - Poisson distribution with rate varying by time
    # More pending pods during peak hours and weekdays
    pending_rate = np.where(
        (df['is_peak_hour']) & (~df['is_weekend']),
        0.5,  # Higher rate during weekday peak
        0.1   # Lower rate otherwise
    )
    df['pending_pods'] = np.random.poisson(pending_rate, n_samples)
    df['pending_pods'] = df['pending_pods'].clip(0, 20)  # Cap at 20

    # Add some periods with high pending pods (scale-up triggers)
    high_pending_indices = np.random.choice(
        n_samples,
        size=int(n_samples * 0.01),  # 1% of time
        replace=False
    )
    for idx in high_pending_indices:
        df.loc[idx, 'pending_pods'] = np.random.randint(5, 15)

    # Worker count - scales with CPU (simulates autoscaling behavior)
    # More workers during high CPU, fewer during low CPU
    df['worker_count'] = 2  # Minimum permanent workers
    df.loc[df['cpu_percent'] > 70, 'worker_count'] = 3
    df.loc[df['cpu_percent'] > 80, 'worker_count'] = 4
    df.loc[df['cpu_percent'] > 85, 'worker_count'] = 5

    # Add some worker count transitions with smoothing
    # Simulate gradual scaling with hysteresis
    for i in range(1, n_samples):
        if df.loc[i, 'worker_count'] != df.loc[i-1, 'worker_count']:
            # Worker count changed - add cooldown period
            cooldown_samples = 10  # 20 minutes
            end_cooldown = min(i + cooldown_samples, n_samples)
            df.loc[i:end_cooldown, 'worker_count'] = df.loc[i, 'worker_count']

    # Ready nodes slightly less than worker count (some provisioning lag)
    df['ready_nodes'] = df['worker_count']
    provisioning_lag = np.random.random(n_samples) < 0.05  # 5% chance of lag
    df.loc[provisioning_lag, 'ready_nodes'] = df.loc[provisioning_lag, 'worker_count'] - 1
    df['ready_nodes'] = df['ready_nodes'].clip(2, None)  # At least 2 ready

    # Total nodes = workers + 1 (master)
    df['total_nodes'] = df['worker_count'] + 1

    # Reorder columns
    df = df[[
        'timestamp', 'cpu_percent', 'memory_percent', 'pending_pods',
        'worker_count', 'ready_nodes', 'total_nodes'
    ]]

    return df


def generate_mock_scaling_history(
    metrics_df: pd.DataFrame,
    scale_up_threshold: float = 70,
    scale_down_threshold: float = 50,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate mock scaling decisions based on metrics.

    Simulates the autoscaler decision-making process to create
    realistic scaling history records.

    Args:
        metrics_df: DataFrame with metrics data
        scale_up_threshold: CPU threshold for scale-up
        scale_down_threshold: CPU threshold for scale-down
        seed: Random seed

    Returns:
        DataFrame with columns: timestamp, decision_id, action,
            cpu_percent, memory_percent, pending_pods, current_nodes,
            target_nodes, reason
    """
    np.random.seed(seed)

    scaling_events = []

    # Simulate scaling decisions every 5 minutes (every ~2-3 samples)
    sample_interval = 5
    sample_indices = range(0, len(metrics_df), sample_interval)

    last_scale_time = None
    scale_up_cooldown = 5  # minutes
    scale_down_cooldown = 15  # minutes

    current_nodes = 2  # Start with minimum

    for i in sample_indices:
        row = metrics_df.iloc[i]
        timestamp = row['timestamp']

        # Check cooldowns
        time_since_last_scale = None
        if last_scale_time is not None:
            time_since_last_scale = (timestamp - last_scale_time).total_seconds() / 60

        can_scale_up = (time_since_last_scale is None or
                        time_since_last_scale >= scale_up_cooldown)
        can_scale_down = (time_since_last_scale is None or
                          time_since_last_scale >= scale_down_cooldown)

        decision_id = f"decision-{i}"

        # Scale-up conditions
        cpu_trigger = row['cpu_percent'] >= scale_up_threshold
        pods_trigger = row['pending_pods'] >= 1

        if can_scale_up and (cpu_trigger or pods_trigger) and current_nodes < 10:
            # Scale up
            target_nodes = min(current_nodes + 1, 10)
            action = 'SCALE_UP'
            reason = f"CPU {row['cpu_percent']:.1f}% >= {scale_up_threshold}% or Pending pods {row['pending_pods']}"

            scaling_events.append({
                'timestamp': timestamp,
                'decision_id': decision_id,
                'action': action,
                'cpu_percent': row['cpu_percent'],
                'memory_percent': row['memory_percent'],
                'pending_pods': row['pending_pods'],
                'current_nodes': current_nodes,
                'target_nodes': target_nodes,
                'reason': reason,
            })

            current_nodes = target_nodes
            last_scale_time = timestamp

        # Scale-down conditions
        elif (can_scale_down and
              row['cpu_percent'] < scale_down_threshold and
              row['memory_percent'] < 50 and
              row['pending_pods'] == 0 and
              current_nodes > 2):

            target_nodes = current_nodes - 1
            action = 'SCALE_DOWN'
            reason = f"CPU {row['cpu_percent']:.1f}% < {scale_down_threshold}%, Memory {row['memory_percent']:.1f}% < 50%, No pending pods"

            scaling_events.append({
                'timestamp': timestamp,
                'decision_id': decision_id,
                'action': action,
                'cpu_percent': row['cpu_percent'],
                'memory_percent': row['memory_percent'],
                'pending_pods': row['pending_pods'],
                'current_nodes': current_nodes,
                'target_nodes': target_nodes,
                'reason': reason,
            })

            current_nodes = target_nodes
            last_scale_time = timestamp

    if scaling_events:
        history_df = pd.DataFrame(scaling_events)
    else:
        # Create empty DataFrame with correct columns
        history_df = pd.DataFrame(columns=[
            'timestamp', 'decision_id', 'action', 'cpu_percent',
            'memory_percent', 'pending_pods', 'current_nodes',
            'target_nodes', 'reason'
        ])

    return history_df


def main():
    """Main entry point for mock data generation."""
    parser = argparse.ArgumentParser(
        description="Generate mock metrics data for ML pipeline testing"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days of data to generate (default: 30)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="ml_training/data",
        help="Output directory for CSV files",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42)",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default="normal",
        choices=["normal", "high-load", "low-load", "volatile"],
        help="Traffic scenario pattern (default: normal)",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("Mock Data Generation for ML Pipeline Testing")
    print("=" * 60)
    print(f"Scenario: {args.scenario}")
    print(f"Duration: {args.days} days")
    print(f"Seed: {args.seed}")

    # Adjust parameters based on scenario
    if args.scenario == "high-load":
        # Higher baseline CPU, more frequent scale-ups
        scale_up_threshold = 60
        print("High-load scenario: Lower scale-up threshold (60%)")
    elif args.scenario == "low-load":
        # Lower baseline CPU, more scale-downs
        scale_up_threshold = 80
        print("Low-load scenario: Higher scale-up threshold (80%)")
    elif args.scenario == "volatile":
        # More spikes, frequent scaling
        scale_up_threshold = 65
        print("Volatile scenario: Frequent scaling patterns")
    else:
        scale_up_threshold = 70

    # Generate metrics
    print("\nGenerating metrics data...")
    metrics_df = generate_mock_data(days=args.days, seed=args.seed)

    # Generate scaling history
    print("Generating scaling history...")
    history_df = generate_mock_scaling_history(
        metrics_df,
        scale_up_threshold=scale_up_threshold
    )

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Save to CSV
    metrics_path = os.path.join(args.output_dir, 'metrics_samples.csv')
    history_path = os.path.join(args.output_dir, 'scaling_history.csv')

    metrics_df.to_csv(metrics_path, index=False)
    history_df.to_csv(history_path, index=False)

    print("\nGenerated mock data:")
    print(f"  Metrics samples: {len(metrics_df)} records")
    print(f"  Scaling history: {len(history_df)} decisions")
    print("\nSaved to:")
    print(f"  {metrics_path}")
    print(f"  {history_path}")

    # Print summary statistics
    print(f"\nMetrics Summary:")
    print(metrics_df.describe())

    if len(history_df) > 0:
        print(f"\nScaling Actions:")
        print(history_df['action'].value_counts())

    print("\n" + "=" * 60)
    print("Mock data generation complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
