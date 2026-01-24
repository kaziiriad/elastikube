"""Validate and backtest trained Prophet forecasting model.

This script loads a trained Prophet model and performs comprehensive
validation including backtesting, scenario analysis, and visualization.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from prophet import Prophet

# Set style
sns.set_style('whitegrid')
plt.rcParams['figure.figsize'] = (14, 8)


class ModelValidator:
    """Validate Prophet forecasting model with backtesting."""

    def __init__(self, model_path: str):
        """Initialize validator with trained model.

        Args:
            model_path: Path to saved Prophet model (JSON)
        """
        self.model_path = model_path
        self.model = Prophet()
        self.model = self.model.from_json(model_path)

        # Load training metrics if available
        metrics_path = model_path.replace('.json', '_metrics.json')
        if os.path.exists(metrics_path):
            with open(metrics_path, 'r') as f:
                self.training_metrics = json.load(f)
        else:
            self.training_metrics = {}

        self.validation_results: Dict = {}

    def backtest_rolling_forecast(
        self,
        df: pd.DataFrame,
        train_periods: int = 720,  # ~1 day at 2-min intervals
        forecast_periods: int = 8,  # 15 minutes ahead at 2-min intervals
    ) -> pd.DataFrame:
        """Perform rolling backtest on historical data.

        Simulates real-time forecasting by training on a window
        and predicting the next periods, then rolling forward.

        Args:
            df: DataFrame with 'ds' and 'y' columns
            train_periods: Initial training window size
            forecast_periods: Number of periods to forecast

        Returns:
            DataFrame with backtest results
        """
        print(f"Running rolling backtest (train={train_periods}, forecast={forecast_periods})...")

        results = []
        total_iterations = (len(df) - train_periods) // forecast_periods

        for i in range(total_iterations):
            # Define train and forecast windows
            train_start = i * forecast_periods
            train_end = train_start + train_periods
            forecast_start = train_end
            forecast_end = forecast_start + forecast_periods

            if forecast_end > len(df):
                break

            # Train on historical window
            train_df = df.iloc[train_start:train_end].copy()
            temp_model = Prophet(
                seasonality_mode=self.model.seasonality_mode,
                daily_seasonality=self.model.daily_seasonality,
                weekly_seasonality=self.model.weekly_seasonality,
                interval_width=0.8,
            )
            temp_model.fit(train_df)

            # Make future dataframe
            future = temp_model.make_future_dataframe(
                periods=forecast_periods,
                freq='2T',
                include_history=False
            )

            # Predict
            forecast = temp_model.predict(future)

            # Get actual values
            actual = df.iloc[forecast_start:forecast_end][['ds', 'y']].copy()

            # Merge predictions with actuals
            merged = actual.merge(forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']], on='ds', how='inner')
            merged['iteration'] = i
            merged['train_end'] = train_df['ds'].max()

            results.append(merged)

            # Progress
            if (i + 1) % 10 == 0:
                print(f"  Completed {i + 1}/{total_iterations} iterations...")

        backtest_df = pd.concat(results, ignore_index=True)
        print(f"Backtest complete: {len(backtest_df)} predictions")
        return backtest_df

    def calculate_metrics(
        self,
        backtest_df: pd.DataFrame,
    ) -> Dict[str, float]:
        """Calculate accuracy metrics from backtest results.

        Args:
            backtest_df: DataFrame with 'y' (actual) and 'yhat' (predicted)

        Returns:
            Dictionary with error metrics
        """
        actual = backtest_df['y']
        predicted = backtest_df['yhat']

        mae = (actual - predicted).abs().mean()
        rmse = ((actual - predicted) ** 2).mean() ** 0.5

        # MAPE (handle zeros)
        actual_nonzero = actual[actual != 0]
        predicted_nonzero = predicted[actual != 0]
        mape = ((actual_nonzero - predicted_nonzero).abs() / actual_nonzero).mean() * 100

        # Bias (average error)
        bias = (predicted - actual).mean()

        # Coverage (how often actual falls within confidence interval)
        within_ci = ((actual >= backtest_df['yhat_lower']) &
                     (actual <= backtest_df['yhat_upper'])).sum() / len(actual)

        metrics = {
            'mae': mae,
            'rmse': rmse,
            'mape': mape,
            'bias': bias,
            'coverage': within_ci * 100,
            'samples': len(backtest_df),
        }

        return metrics

    def analyze_by_time_period(
        self,
        backtest_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """Analyze prediction accuracy by time period.

        Break down metrics by:
        - Peak vs off-peak hours
        - Weekday vs weekend
        - Hour of day

        Args:
            backtest_df: DataFrame with backtest results

        Returns:
            DataFrame with metrics by category
        """
        df = backtest_df.copy()
        df['hour'] = df['ds'].dt.hour
        df['day_of_week'] = df['ds'].dt.dayofweek
        df['is_weekend'] = df['day_of_week'] >= 5
        df['is_peak'] = (df['hour'] >= 9) & (df['hour'] < 21)

        results = []

        # Overall
        overall = self.calculate_metrics(df)
        overall['category'] = 'Overall'
        overall['subcategory'] = 'All'
        results.append(overall)

        # Peak vs Off-peak
        for is_peak, name in [(True, 'Peak (9AM-9PM)'), (False, 'Off-Peak (9PM-9AM)')]:
            subset = df[df['is_peak'] == is_peak]
            if len(subset) > 0:
                metrics = self.calculate_metrics(subset)
                metrics['category'] = 'Time of Day'
                metrics['subcategory'] = name
                results.append(metrics)

        # Weekday vs Weekend
        for is_weekend, name in [(True, 'Weekend'), (False, 'Weekday')]:
            subset = df[df['is_weekend'] == is_weekend]
            if len(subset) > 0:
                metrics = self.calculate_metrics(subset)
                metrics['category'] = 'Day Type'
                metrics['subcategory'] = name
                results.append(metrics)

        # Hourly breakdown
        for hour in range(24):
            subset = df[df['hour'] == hour]
            if len(subset) > 10:  # Only if enough samples
                metrics = self.calculate_metrics(subset)
                metrics['category'] = 'Hour of Day'
                metrics['subcategory'] = f'{hour:02d}:00'
                results.append(metrics)

        return pd.DataFrame(results)

    def analyze_prediction_intervals(
        self,
        backtest_df: pd.DataFrame,
    ) -> Dict:
        """Analyze prediction interval coverage.

        Check how often actual values fall within predicted intervals.

        Args:
            backtest_df: DataFrame with backtest results

        Returns:
            Dictionary with interval analysis
        """
        df = backtest_df.copy()

        # Calculate error relative to interval width
        df['interval_width'] = df['yhat_upper'] - df['yhat_lower']
        df['error'] = (df['y'] - df['yhat']).abs()
        df['error_as_pct_of_interval'] = (df['error'] / df['interval_width']) * 100

        # Count how often actual falls outside interval
        outside_above = (df['y'] > df['yhat_upper']).sum()
        outside_below = (df['y'] < df['yhat_lower']).sum()
        within = ((df['y'] >= df['yhat_lower']) & (df['y'] <= df['yhat_upper'])).sum()

        # Worst predictions (largest errors)
        worst_predictions = df.nlargest(10, 'error_as_pct_of_interval')[
            ['ds', 'y', 'yhat', 'error', 'interval_width']
        ].to_dict('records')

        analysis = {
            'total_predictions': len(df),
            'within_interval': within,
            'within_interval_pct': (within / len(df)) * 100,
            'outside_above': outside_above,
            'outside_below': outside_below,
            'mean_interval_width': df['interval_width'].mean(),
            'mean_error': df['error'].mean(),
            'worst_predictions': worst_predictions,
        }

        return analysis

    def plot_backtest_results(
        self,
        backtest_df: pd.DataFrame,
        output_path: str,
    ) -> None:
        """Create visualizations of backtest results.

        Args:
            backtest_df: DataFrame with backtest results
            output_path: Path to save plot
        """
        fig, axes = plt.subplots(3, 1, figsize=(14, 12))

        # Plot 1: Actual vs Predicted (sample)
        sample = backtest_df.sample(n=min(500, len(backtest_df))).sort_values('ds')
        axes[0].plot(sample['ds'], sample['y'], 'o', label='Actual', alpha=0.5, markersize=4)
        axes[0].plot(sample['ds'], sample['yhat'], '-', label='Predicted', alpha=0.8)
        axes[0].fill_between(
            sample['ds'],
            sample['yhat_lower'],
            sample['yhat_upper'],
            alpha=0.2,
            label='80% CI'
        )
        axes[0].set_title('Backtest: Actual vs Predicted CPU', fontsize=14, fontweight='bold')
        axes[0].set_ylabel('CPU (%)', fontsize=12)
        axes[0].legend()
        axes[0].grid(True, alpha=0.3)

        # Plot 2: Residuals over time
        sample['residual'] = sample['y'] - sample['yhat']
        axes[1].plot(sample['ds'], sample['residual'], 'o', alpha=0.5, markersize=4)
        axes[1].axhline(y=0, color='red', linestyle='--')
        axes[1].set_title('Prediction Residuals (Actual - Predicted)', fontsize=14, fontweight='bold')
        axes[1].set_ylabel('Residual (%)', fontsize=12)
        axes[1].grid(True, alpha=0.3)

        # Plot 3: Error distribution
        axes[2].hist(sample['residual'], bins=50, edgecolor='black', alpha=0.7)
        axes[2].axvline(x=0, color='red', linestyle='--', linewidth=2)
        axes[2].axvline(x=sample['residual'].mean(), color='orange',
                       linestyle='--', linewidth=2, label=f'Mean: {sample["residual"].mean():.2f}')
        axes[2].set_title('Error Distribution', fontsize=14, fontweight='bold')
        axes[2].set_xlabel('Residual (%)', fontsize=12)
        axes[2].set_ylabel('Count', fontsize=12)
        axes[2].legend()
        axes[2].grid(True, alpha=0.3)

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        print(f"Plot saved to {output_path}")
        plt.close()

    def plot_metrics_by_period(
        self,
        metrics_df: pd.DataFrame,
        output_path: str,
    ) -> None:
        """Plot metrics by time period.

        Args:
            metrics_df: DataFrame from analyze_by_time_period
            output_path: Path to save plot
        """
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))

        # Filter relevant categories
        time_of_day = metrics_df[metrics_df['category'] == 'Time of Day']
        day_type = metrics_df[metrics_df['category'] == 'Day Type']
        hourly = metrics_df[metrics_df['category'] == 'Hour of Day'].sort_values('subcategory')

        # MAE by time period
        axes[0, 0].bar(time_of_day['subcategory'], time_of_day['mae'], color=['#1f77b4', '#ff7f0e'], alpha=0.7)
        axes[0, 0].set_title('MAE by Time of Day', fontsize=12, fontweight='bold')
        axes[0, 0].set_ylabel('MAE (%)', fontsize=10)
        axes[0, 0].tick_params(axis='x', rotation=15)

        # MAE by day type
        axes[0, 1].bar(day_type['subcategory'], day_type['mae'], color=['#2ca02c', '#d62728'], alpha=0.7)
        axes[0, 1].set_title('MAE by Day Type', fontsize=12, fontweight='bold')
        axes[0, 1].set_ylabel('MAE (%)', fontsize=10)

        # MAPE by time period
        axes[1, 0].bar(time_of_day['subcategory'], time_of_day['mape'], color=['#1f77b4', '#ff7f0e'], alpha=0.7)
        axes[1, 0].set_title('MAPE by Time of Day', fontsize=12, fontweight='bold')
        axes[1, 0].set_ylabel('MAPE (%)', fontsize=10)
        axes[1, 0].tick_params(axis='x', rotation=15)

        # Hourly MAE
        if len(hourly) > 0:
            axes[1, 1].plot(range(24), [0] * 24, color='gray', alpha=0.3)  # Baseline
            axes[1, 1].bar(
                hourly['subcategory'].str.extract(r'(\d+)')[0].astype(int),
                hourly['mae'],
                color='#9467bd',
                alpha=0.7
            )
            axes[1, 1].axvline(x=9, color='red', linestyle='--', alpha=0.5, label='Peak start')
            axes[1, 1].axvline(x=21, color='red', linestyle='--', alpha=0.5, label='Peak end')
            axes[1, 1].set_title('MAE by Hour', fontsize=12, fontweight='bold')
            axes[1, 1].set_xlabel('Hour', fontsize=10)
            axes[1, 1].set_ylabel('MAE (%)', fontsize=10)
            axes[1, 1].legend()

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        print(f"Plot saved to {output_path}")
        plt.close()

    def save_validation_report(
        self,
        output_path: str,
    ) -> None:
        """Save validation results to JSON.

        Args:
            output_path: Path to save report
        """
        # Convert any DataFrame results to dict
        report = {}
        for key, value in self.validation_results.items():
            if isinstance(value, pd.DataFrame):
                report[key] = value.to_dict('records')
            elif isinstance(value, dict):
                # Handle nested dicts (like interval analysis)
                report[key] = {
                    k: v.tolist() if isinstance(v, pd.Series) else v
                    for k, v in value.items()
                }
            else:
                report[key] = value

        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        print(f"Validation report saved to {output_path}")


def main():
    """Main entry point for model validation."""
    parser = argparse.ArgumentParser(
        description="Validate Prophet forecasting model"
    )
    parser.add_argument(
        "--model-path",
        type=str,
        required=True,
        help="Path to trained Prophet model (JSON)",
    )
    parser.add_argument(
        "--data-path",
        type=str,
        default="ml_training/data/metrics_samples.csv",
        help="Path to metrics CSV file",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="ml_training/validation",
        help="Output directory for validation results",
    )
    parser.add_argument(
        "--train-periods",
        type=int,
        default=720,
        help="Training window size for backtest (2-min periods)",
    )
    parser.add_argument(
        "--forecast-periods",
        type=int,
        default=8,
        help="Forecast periods for backtest (2-min periods = 15 min)",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("Prophet Model Validation")
    print("=" * 60)

    # Load data
    print(f"\nLoading data from {args.data_path}...")
    df = pd.read_csv(args.data_path, parse_dates=['timestamp'])
    df = df.rename(columns={'timestamp': 'ds', 'cpu_percent': 'y'})
    df = df[['ds', 'y']].dropna()
    df = df.sort_values('ds').reset_index(drop=True)
    print(f"Loaded {len(df)} records")

    # Create validator
    validator = ModelValidator(args.model_path)

    # Run backtest
    print("\n" + "=" * 60)
    print("Running Backtest")
    print("=" * 60)
    backtest_df = validator.backtest_rolling_forecast(
        df,
        train_periods=args.train_periods,
        forecast_periods=args.forecast_periods,
    )
    validator.validation_results['backtest'] = backtest_df

    # Calculate overall metrics
    print("\n" + "=" * 60)
    print("Overall Metrics")
    print("=" * 60)
    overall_metrics = validator.calculate_metrics(backtest_df)
    for key, value in overall_metrics.items():
        if key != 'samples':
            print(f"  {key.upper()}: {value:.2f}")
    validator.validation_results['overall_metrics'] = overall_metrics

    # Analyze by time period
    print("\n" + "=" * 60)
    print("Analysis by Time Period")
    print("=" * 60)
    metrics_by_period = validator.analyze_by_time_period(backtest_df)
    print("\nKey Findings:")
    print(metrics_by_period[metrics_by_period['category'].isin(['Time of Day', 'Day Type'])][
        ['subcategory', 'mae', 'rmse', 'mape']
    ].to_string(index=False))
    validator.validation_results['metrics_by_period'] = metrics_by_period

    # Analyze prediction intervals
    print("\n" + "=" * 60)
    print("Prediction Interval Analysis")
    print("=" * 60)
    interval_analysis = validator.analyze_prediction_intervals(backtest_df)
    print(f"Coverage: {interval_analysis['within_interval_pct']:.1f}% within 80% CI")
    print(f"Outside above: {interval_analysis['outside_above']}")
    print(f"Outside below: {interval_analysis['outside_below']}")
    validator.validation_results['interval_analysis'] = interval_analysis

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Generate plots
    print("\n" + "=" * 60)
    print("Generating Visualizations")
    print("=" * 60)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    validator.plot_backtest_results(
        backtest_df,
        os.path.join(args.output_dir, f'backtest_results_{timestamp}.png')
    )
    validator.plot_metrics_by_period(
        metrics_by_period,
        os.path.join(args.output_dir, f'metrics_by_period_{timestamp}.png')
    )

    # Save report
    validator.save_validation_report(
        os.path.join(args.output_dir, f'validation_report_{timestamp}.json')
    )

    print("\n" + "=" * 60)
    print("Validation Complete")
    print("=" * 60)
    print(f"Results saved to {args.output_dir}")


if __name__ == "__main__":
    main()
