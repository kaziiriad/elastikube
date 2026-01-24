"""Train Prophet model for predictive CPU scaling.

This script trains a Prophet model to predict future CPU usage
based on historical metrics. The trained model can be used
to proactively scale worker nodes before load spikes occur.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3
import pandas as pd
from prophet import Prophet
from prophet.diagnostics import cross_validation, performance_metrics
from prophet.serialize import model_to_json

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils.feature_engineer import FeatureEngineer


class CPUForecaster:
    """Train and manage Prophet forecasting model for CPU prediction."""

    def __init__(
        self,
        prediction_horizon_minutes: int = 15,
        seasonality_mode: str = "multiplicative",
        daily_seasonality: bool = True,
        weekly_seasonality: bool = True,
    ):
        """Initialize the CPU forecaster.

        Args:
            prediction_horizon_minutes: Minutes ahead to predict
            seasonality_mode: 'additive' or 'multiplicative'
            daily_seasonality: Enable daily seasonality
            weekly_seasonality: Enable weekly seasonality
        """
        self.prediction_horizon_minutes = prediction_horizon_minutes
        self.model = Prophet(
            seasonality_mode=seasonality_mode,
            daily_seasonality=daily_seasonality,
            weekly_seasonality=weekly_seasonality,
            # Additional settings for robust forecasting
            changepoint_prior_scale=0.05,  # Default: 0.05 (lower = less flexible)
            seasonality_prior_scale=10.0,  # Default: 10.0
            holidays_prior_scale=10.0,  # Default: 10.0
            interval_width=0.8,  # 80% confidence interval
            mcmc_samples=0,  # Disable MCMC for faster training
        )
        self.feature_engineer = FeatureEngineer()
        self.training_metrics: Dict[str, float] = {}

    def prepare_data(
        self,
        df: pd.DataFrame,
        target_col: str = 'cpu_percent',
    ) -> Tuple[pd.DataFrame, Optional[pd.DataFrame]]:
        """Prepare data for Prophet training.

        Prophet requires columns named 'ds' (timestamp) and 'y' (value to predict).

        Args:
            df: Raw metrics DataFrame
            target_col: Column to predict

        Returns:
            Tuple of (prophet_df, feature_df)
            - prophet_df: DataFrame with 'ds' and 'y' columns for Prophet
            - feature_df: DataFrame with engineered features (optional, for analysis)
        """
        df = df.copy()

        # Ensure timestamp is datetime
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])

        # Remove rows with missing target
        df = df.dropna(subset=[target_col])

        # Sort by timestamp
        df = df.sort_values('timestamp').reset_index(drop=True)

        # Remove duplicate timestamps (keep first occurrence)
        # Prophet requires unique timestamps
        before_dedup = len(df)
        df = df.drop_duplicates(subset=['timestamp'], keep='first')
        if len(df) < before_dedup:
            print(f"Removed {before_dedup - len(df)} duplicate timestamps")

        # Reset index after deduplication to ensure clean index
        df = df.reset_index(drop=True)

        # Create Prophet format (ds, y)
        # Prophet requires timezone-naive datetime
        timestamps = df['timestamp'].copy()
        # Remove timezone info if present
        if timestamps.dt.tz is not None:
            timestamps = timestamps.dt.tz_localize(None)

        prophet_df = pd.DataFrame({
            'ds': timestamps,
            'y': df[target_col].values
        })

        # Add additional regressors (optional features Prophet can use)
        # These are time-varying external features
        # Use .values to avoid index alignment issues
        if 'pending_pods' in df.columns:
            prophet_df['pending_pods'] = df['pending_pods'].fillna(0).values
        if 'worker_count' in df.columns:
            mode_val = df['worker_count'].mode()[0] if len(df['worker_count'].mode()) > 0 else 2
            prophet_df['worker_count'] = df['worker_count'].fillna(mode_val).values

        # Engineer features for analysis
        feature_df = self.feature_engineer.create_feature_set(
            df,
            target_col=target_col,
            prediction_horizon_minutes=self.prediction_horizon_minutes
        )

        return prophet_df, feature_df

    def train(
        self,
        df: pd.DataFrame,
        validation_split: float = 0.2,
    ) -> Dict[str, Any]:
        """Train the Prophet model.

        Args:
            df: DataFrame with 'ds' and 'y' columns
            validation_split: Fraction of data to hold out for validation

        Returns:
            Dictionary with training metrics
        """
        # Split data
        split_idx = int(len(df) * (1 - validation_split))
        train_df = df.iloc[:split_idx].copy()
        val_df = df.iloc[split_idx:].copy()

        print(f"Training set: {len(train_df)} samples")
        print(f"Validation set: {len(val_df)} samples")

        # Add regressors if available
        regressors = [col for col in df.columns if col not in ['ds', 'y']]
        if regressors:
            print(f"Adding regressors: {regressors}")
        for regressor in regressors:
            self.model.add_regressor(regressor, mode='additive')

        # Fit model
        print("Training Prophet model...")
        self.model.fit(train_df)

        # Make predictions on validation set
        print("Validating model...")
        future = self.model.make_future_dataframe(
            periods=len(val_df),
            freq='2min',  # 2-minute intervals
            include_history=False
        )

        # Add regressor values to future dataframe
        for regressor in regressors:
            # Merge actual regressor values
            future = future.merge(
                val_df[['ds', regressor]],
                on='ds',
                how='left'
            )
            # Forward fill any missing values
            future[regressor] = future[regressor].ffill().fillna(0)

        # Predict
        forecast = self.model.predict(future)

        # Calculate metrics
        val_df_merged = val_df.merge(forecast[['ds', 'yhat']], on='ds', how='inner')

        if len(val_df_merged) > 0:
            mae = self._calculate_mae(val_df_merged['y'], val_df_merged['yhat'])
            rmse = self._calculate_rmse(val_df_merged['y'], val_df_merged['yhat'])
            mape = self._calculate_mape(val_df_merged['y'], val_df_merged['yhat'])

            self.training_metrics = {
                'mae': mae,
                'rmse': rmse,
                'mape': mape,
                'validation_samples': len(val_df_merged),
                'training_samples': len(train_df),
            }

            print(f"\nValidation Metrics:")
            print(f"  MAE:  {mae:.2f} percentage points")
            print(f"  RMSE: {rmse:.2f} percentage points")
            print(f"  MAPE: {mape:.2f}%")

        return self.training_metrics

    def cross_validate(
        self,
        df: pd.DataFrame,
        initial: str = '7 days',
        period: str = '1 day',
        horizon: str = '1 day',
    ) -> pd.DataFrame:
        """Perform time-series cross-validation.

        Args:
            df: Training data with 'ds' and 'y'
            initial: Initial training period
            period: Spacing between cutoff dates
            horizon: Forecast horizon

        Returns:
            DataFrame with CV performance metrics
        """
        print(f"Running cross-validation (initial={initial}, period={period}, horizon={horizon})...")

        cv_results = cross_validation(
            self.model,
            initial=initial,
            period=period,
            horizon=horizon,
            parallel='threads',
        )

        # Calculate performance metrics
        perf = performance_metrics(cv_results)

        print("\nCross-Validation Performance:")
        print(perf[['horizon', 'mse', 'rmse', 'mae', 'mape']].to_string(index=False))

        return perf

    def predict(
        self,
        periods: int,
        freq: str = '2min',
        include_history: bool = False,
    ) -> pd.DataFrame:
        """Make future predictions.

        Args:
            periods: Number of periods to predict
            freq: Frequency of predictions (e.g., '2min' for 2 minutes)
            include_history: Include historical predictions

        Returns:
            DataFrame with predictions ('yhat', 'yhat_lower', 'yhat_upper')
        """
        future = self.model.make_future_dataframe(
            periods=periods,
            freq=freq,
            include_history=include_history
        )
        forecast = self.model.predict(future)
        return forecast

    def save_model(self, output_path: str) -> None:
        """Save trained model to file.

        Args:
            output_path: Path to save model (JSON format)
        """
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Serialize model to JSON using Prophet 1.2+ API
        model_json = model_to_json(self.model)

        with open(output_path, 'w') as f:
            f.write(model_json)

        # Save training metrics
        metrics_path = output_path.replace('.json', '_metrics.json')
        with open(metrics_path, 'w') as f:
            json.dump(self.training_metrics, f, indent=2)

        print(f"Model saved to {output_path}")
        print(f"Metrics saved to {metrics_path}")

    def upload_to_s3(
        self,
        model_path: str,
        bucket: str,
        key: str,
        profile: Optional[str] = None,
    ) -> str:
        """Upload model to S3.

        Args:
            model_path: Local path to model file
            bucket: S3 bucket name
            key: S3 key (path)
            profile: AWS profile name

        Returns:
            S3 URI
        """
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        s3 = session.client('s3')

        print(f"Uploading model to s3://{bucket}/{key}...")
        s3.upload_file(model_path, bucket, key)

        s3_uri = f"s3://{bucket}/{key}"
        print(f"Model uploaded: {s3_uri}")
        return s3_uri

    @staticmethod
    def _calculate_mae(actual: pd.Series, predicted: pd.Series) -> float:
        """Calculate Mean Absolute Error."""
        return (actual - predicted).abs().mean()

    @staticmethod
    def _calculate_rmse(actual: pd.Series, predicted: pd.Series) -> float:
        """Calculate Root Mean Squared Error."""
        return ((actual - predicted) ** 2).mean() ** 0.5

    @staticmethod
    def _calculate_mape(actual: pd.Series, predicted: pd.Series) -> float:
        """Calculate Mean Absolute Percentage Error."""
        # Avoid division by zero
        actual_nonzero = actual[actual != 0]
        predicted_nonzero = predicted[actual != 0]
        return ((actual_nonzero - predicted_nonzero).abs() / actual_nonzero).mean() * 100


def main():
    """Main entry point for model training."""
    parser = argparse.ArgumentParser(
        description="Train Prophet model for CPU prediction"
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
        default="ml_training/models",
        help="Output directory for model files",
    )
    parser.add_argument(
        "--horizon-minutes",
        type=int,
        default=15,
        help="Prediction horizon in minutes",
    )
    parser.add_argument(
        "--validation-split",
        type=float,
        default=0.2,
        help="Validation split fraction",
    )
    parser.add_argument(
        "--s3-bucket",
        type=str,
        default=None,
        help="S3 bucket to upload model (optional)",
    )
    parser.add_argument(
        "--s3-key",
        type=str,
        default=None,
        help="S3 key for model upload (optional)",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="AWS profile for S3 upload",
    )
    parser.add_argument(
        "--cross-validate",
        action="store_true",
        help="Run time-series cross-validation",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("CPU Forecasting Model Training")
    print("=" * 60)

    # Load data
    print(f"\nLoading data from {args.data_path}...")
    if not os.path.exists(args.data_path):
        print(f"Error: Data file not found: {args.data_path}")
        print("Run extract_data.py first to generate training data.")
        sys.exit(1)

    df = pd.read_csv(args.data_path, parse_dates=['timestamp'])
    print(f"Loaded {len(df)} records")
    print(f"Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")

    # Create forecaster
    forecaster = CPUForecaster(
        prediction_horizon_minutes=args.horizon_minutes,
        seasonality_mode='multiplicative',  # CPU patterns scale with load
        daily_seasonality=True,
        weekly_seasonality=True,
    )

    # Prepare data
    print("\nPreparing data for Prophet...")
    prophet_df, feature_df = forecaster.prepare_data(df, target_col='cpu_percent')
    print(f"Prophet format: {len(prophet_df)} samples")

    # Train model
    print("\n" + "=" * 60)
    print("Training Model")
    print("=" * 60)
    metrics = forecaster.train(
        prophet_df,
        validation_split=args.validation_split,
    )

    # Cross-validation (optional)
    if args.cross_validate:
        print("\n" + "=" * 60)
        print("Cross-Validation")
        print("=" * 60)
        cv_results = forecaster.cross_validate(prophet_df)

    # Save model
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    model_filename = f"cpu_prophet_model_{timestamp}.json"
    model_path = os.path.join(args.output_dir, model_filename)

    print("\n" + "=" * 60)
    print("Saving Model")
    print("=" * 60)
    forecaster.save_model(model_path)

    # Upload to S3 (optional)
    if args.s3_bucket and args.s3_key:
        s3_key = args.s3_key.replace('{timestamp}', timestamp)
        s3_uri = forecaster.upload_to_s3(
            model_path,
            args.s3_bucket,
            s3_key,
            args.profile,
        )
        print(f"\nS3 URI: {s3_uri}")

    # Print summary
    print("\n" + "=" * 60)
    print("Training Summary")
    print("=" * 60)
    print(f"Model: {model_filename}")
    print(f"Prediction horizon: {args.horizon_minutes} minutes")
    print(f"Validation MAE: {metrics.get('mae', 'N/A'):.2f} percentage points")
    print(f"Validation RMSE: {metrics.get('rmse', 'N/A'):.2f} percentage points")
    print(f"Validation MAPE: {metrics.get('mape', 'N/A'):.2f}%")
    print("\nModel training complete!")


if __name__ == "__main__":
    main()
