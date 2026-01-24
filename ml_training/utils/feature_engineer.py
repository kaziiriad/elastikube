"""Feature engineering for predictive scaling model.

Creates temporal features, lag features, and rolling statistics
for time-series forecasting of CPU/memory metrics.
"""

import numpy as np
import pandas as pd
from typing import List, Tuple


class FeatureEngineer:
    """Engineer features for time-series forecasting."""

    # Time-aware thresholds (must match production config)
    PEAK_HOUR_START = 9  # 9 AM
    PEAK_HOUR_END = 21    # 9 PM

    def __init__(self):
        """Initialize the feature engineer."""
        pass

    def add_temporal_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add time-based cyclical features.

        Uses sine/cosine encoding to preserve cyclical nature of time
        (e.g., 23:00 is close to 00:00, not far away).

        Args:
            df: DataFrame with 'timestamp' column

        Returns:
            DataFrame with added temporal features
        """
        df = df.copy()

        # Ensure timestamp is datetime
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])

        # Extract time components
        df['hour'] = df['timestamp'].dt.hour
        df['day_of_week'] = df['timestamp'].dt.dayofweek
        df['day_of_month'] = df['timestamp'].dt.day
        df['minute'] = df['timestamp'].dt.minute

        # Cyclical encoding for hour (0-23)
        # This preserves that 23:00 is close to 00:00
        df['hour_sin'] = np.sin(2 * np.pi * df['hour'] / 24)
        df['hour_cos'] = np.cos(2 * np.pi * df['hour'] / 24)

        # Cyclical encoding for day of week (0=Mon, 6=Sun)
        df['dow_sin'] = np.sin(2 * np.pi * df['day_of_week'] / 7)
        df['dow_cos'] = np.cos(2 * np.pi * df['day_of_week'] / 7)

        # Boolean flags
        df['is_peak_hour'] = ((df['hour'] >= self.PEAK_HOUR_START) &
                                (df['hour'] < self.PEAK_HOUR_END)).astype(int)
        df['is_weekend'] = (df['day_of_week'] >= 5).astype(int)
        df['is_business_hours'] = (df['hour'].between(9, 17)).astype(int)

        return df

    def add_lag_features(
        self,
        df: pd.DataFrame,
        value_col: str,
        lags: List[int] = [1, 3, 5, 10, 15, 30]
    ) -> pd.DataFrame:
        """Add lag features (past values) for time series.

        Lag features capture recent history - e.g., CPU 5 minutes ago.

        Args:
            df: DataFrame sorted by timestamp
            value_col: Column to create lags for (e.g., 'cpu_percent')
            lags: List of lag periods (in rows, assumes 2-min samples)

        Returns:
            DataFrame with added lag columns
        """
        df = df.copy()

        for lag in lags:
            # Shift by lag periods (2-min samples)
            # Lag 1 = 2 minutes ago, Lag 5 = 10 minutes ago
            lag_col_name = f'{value_col}_lag_{lag}'
            df[lag_col_name] = df[value_col].shift(lag)

        return df

    def add_rolling_features(
        self,
        df: pd.DataFrame,
        value_col: str,
        windows: List[int] = [5, 10, 15, 30]
    ) -> pd.DataFrame:
        """Add rolling window features (moving stats).

        Rolling features capture trends over recent time windows.

        Args:
            df: DataFrame sorted by timestamp
            value_col: Column to compute rolling stats for
            windows: List of window sizes (in rows, 2-min samples)

        Returns:
            DataFrame with added rolling features
        """
        df = df.copy()

        for window in windows:
            # Rolling mean (trend)
            df[f'{value_col}_rolling_mean_{window}'] = (
                df[value_col].rolling(window=window, min_periods=1).mean()
            )

            # Rolling std (volatility)
            df[f'{value_col}_rolling_std_{window}'] = (
                df[value_col].rolling(window=window, min_periods=1).std()
            )

            # Rolling min/max (range)
            df[f'{value_col}_rolling_min_{window}'] = (
                df[value_col].rolling(window=window, min_periods=1).min()
            )
            df[f'{value_col}_rolling_max_{window}'] = (
                df[value_col].rolling(window=window, min_periods=1).max()
            )

        return df

    def add_trend_features(
        self,
        df: pd.DataFrame,
        value_col: str,
        periods: List[int] = [5, 10, 15]
    ) -> pd.DataFrame:
        """Add trend/difference features.

        Trends capture the direction and rate of change.

        Args:
            df: DataFrame sorted by timestamp
            value_col: Column to compute trends for
            periods: List of periods to compute difference over

        Returns:
            DataFrame with added trend features
        """
        df = df.copy()

        for period in periods:
            # Difference from N periods ago
            df[f'{value_col}_diff_{period}'] = df[value_col].diff(periods=period)

            # Percent change from N periods ago
            df[f'{value_col}_pct_change_{period}'] = (
                df[value_col].pct_change(periods=period)
            )

        return df

    def add_pending_pod_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add pending pod indicator features.

        Pending pods are a leading indicator - they indicate
        upcoming resource demand.

        Args:
            df: DataFrame with 'pending_pods' column

        Returns:
            DataFrame with added pending pod features
        """
        df = df.copy()

        # Binary indicator: any pending pods?
        df['has_pending_pods'] = (df['pending_pods'] > 0).astype(int)

        # High pending pods threshold
        df['high_pending_pods'] = (df['pending_pods'] >= 5).astype(int)

        return df

    def create_feature_set(
        self,
        df: pd.DataFrame,
        target_col: str = 'cpu_percent',
        prediction_horizon_minutes: int = 15
    ) -> pd.DataFrame:
        """Create complete feature set for model training.

        Args:
            df: Raw metrics DataFrame
            target_col: Column to predict (default: cpu_percent)
            prediction_horizon_minutes: Minutes ahead to predict (for creating target)

        Returns:
            DataFrame with all features and shifted target
        """
        df = df.copy().reset_index(drop=True)

        # Sort by timestamp
        df = df.sort_values('timestamp').reset_index(drop=True)

        # Add temporal features
        df = self.add_temporal_features(df)

        # Add lag features (recent history)
        df = self.add_lag_features(df, target_col, lags=[1, 3, 5, 10, 15])

        # Add rolling features (trends, volatility)
        df = self.add_rolling_features(df, target_col, windows=[5, 10, 15, 30])

        # Add trend features (rate of change)
        df = self.add_trend_features(df, target_col, periods=[5, 10, 15])

        # Add pending pod features (leading indicator)
        if 'pending_pods' in df.columns:
            df = self.add_pending_pod_features(df)

        # Create target variable: CPU X minutes in the future
        # 2-minute sample rate, so horizon of 15 min = 7.5 periods ≈ 8 periods
        periods_ahead = prediction_horizon_minutes // 2
        df[f'{target_col}_future'] = df[target_col].shift(-periods_ahead)

        # Drop rows with NaN (from lag/shift operations)
        df = df.dropna()

        return df

    def get_feature_importance_info(self) -> dict:
        """Return information about features for documentation.

        Returns:
            Dictionary with feature descriptions
        """
        return {
            'temporal': {
                'hour_sin, hour_cos': 'Time of day (cyclical encoding)',
                'dow_sin, dow_cos': 'Day of week (cyclical encoding)',
                'is_peak_hour': '9 AM - 9 PM flag',
                'is_weekend': 'Saturday/Sunday flag',
                'is_business_hours': '9 AM - 5 PM flag',
            },
            'lag': {
                'cpu_percent_lag_1': 'CPU 2 min ago',
                'cpu_percent_lag_3': 'CPU 6 min ago',
                'cpu_percent_lag_5': 'CPU 10 min ago',
                'cpu_percent_lag_10': 'CPU 20 min ago',
                'cpu_percent_lag_15': 'CPU 30 min ago',
            },
            'rolling': {
                'rolling_mean_*': 'Average CPU over window (trend)',
                'rolling_std_*': 'CPU volatility over window',
                'rolling_min/max_*': 'CPU range over window',
            },
            'trend': {
                'diff_*': 'CPU change over N periods',
                'pct_change_*': 'CPU % change over N periods',
            },
            'leading_indicators': {
                'has_pending_pods': 'Any pods pending',
                'high_pending_pods': '5+ pods pending',
            }
        }


def main():
    """Test the feature engineer with sample data."""
    # Create sample data
    dates = pd.date_range(
        start='2024-01-01',
        periods=1000,
        freq='2T'  # 2-minute intervals
    )

    sample_df = pd.DataFrame({
        'timestamp': dates,
        'cpu_percent': np.random.uniform(20, 90, 1000),
        'memory_percent': np.random.uniform(30, 80, 1000),
        'pending_pods': np.random.poisson(0.5, 1000),
        'worker_count': 2,
        'ready_nodes': 3,
        'total_nodes': 3,
    })

    # Add trend pattern to CPU
    sample_df['cpu_percent'] += 20 * np.sin(np.linspace(0, 8*np.pi, 1000))

    # Create feature engineer
    engineer = FeatureEngineer()

    # Create features
    features_df = engineer.create_feature_set(
        sample_df,
        target_col='cpu_percent',
        prediction_horizon_minutes=15
    )

    print("Feature Engineering Test")
    print("=" * 50)
    print(f"Original records: {len(sample_df)}")
    print(f"After feature engineering: {len(features_df)}")
    print(f"Features created: {len(features_df.columns)}")

    print("\nFeature columns:")
    for i, col in enumerate(features_df.columns, 1):
        print(f"  {i}. {col}")

    print("\nSample data:")
    print(features_df[['timestamp', 'cpu_percent', 'cpu_percent_future',
                       'hour_sin', 'hour_cos', 'cpu_percent_lag_5',
                       'cpu_percent_rolling_mean_10']].head())


if __name__ == '__main__':
    main()
