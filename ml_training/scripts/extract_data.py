"""DynamoDB data extractor for ML training.

Extracts historical metrics and scaling decisions from DynamoDB tables
for predictive scaling model training.
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import boto3
import pandas as pd


class DynamoDBExtractor:
    """Extract data from DynamoDB tables for ML training."""

    def __init__(
        self,
        region: str = "ap-southeast-1",
        profile: Optional[str] = None,
    ):
        """Initialize the DynamoDB extractor.

        Args:
            region: AWS region
            profile: AWS profile name (optional)
        """
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self.dynamodb = session.resource("dynamodb", region_name=region)
        self.client = session.client("dynamodb", region_name=region)

    def extract_metrics_samples(
        self,
        table_name: str,
        days: int = 30,
        profile: Optional[str] = None,
    ) -> pd.DataFrame:
        """Extract metrics samples from DynamoDB.

        Args:
            table_name: DynamoDB table name (default: k3s-scaling-metrics-samples)
            days: Number of days to extract
            profile: AWS profile for direct query

        Returns:
            DataFrame with columns: timestamp, cpu_percent, memory_percent,
                pending_pods, worker_count, ready_nodes, total_nodes
        """
        table = self.dynamodb.Table(table_name)

        # Calculate start time
        start_time = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        print(f"Extracting metrics samples from {table_name}...")
        print(f"Time range: {start_time} to now")

        # Scan with filter (note: ScanFilter is deprecated, use FilterExpression)
        # The Table resource API takes raw Python values, not DynamoDB type
        # descriptors ({"S": "..."}). Wrapping in {S: ...} makes boto3 send
        # operand type M to DynamoDB and the filter rejects it.
        records = []
        scan_kwargs = {
            "FilterExpression": "#ts >= :start_time",
            "ExpressionAttributeNames": {"#ts": "timestamp"},
            "ExpressionAttributeValues": {":start_time": start_time},
        }

        try:
            response = table.scan(**scan_kwargs)
            records.extend(response.get("Items", []))

            # Handle pagination
            while "LastEvaluatedKey" in response:
                scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = table.scan(**scan_kwargs)
                records.extend(response.get("Items", []))

        except Exception as e:
            print(f"Error scanning table: {e}")
            print("Table may not exist or be empty. Returning empty DataFrame.")
            return pd.DataFrame()

        if not records:
            print(f"No records found in {table_name}")
            return pd.DataFrame()

        # Convert to DataFrame
        df = pd.DataFrame(records)

        # Parse JSON fields and flatten
        if "metrics" in df.columns:
            metrics_df = pd.json_normalize(df["metrics"].tolist())
            df = pd.concat([df.drop(columns=["metrics"]), metrics_df], axis=1)

        # Parse timestamp
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])

        # Sort by timestamp
        df = df.sort_values("timestamp").reset_index(drop=True)

        print(f"Extracted {len(df)} records")
        print(f"Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")

        return df

    def extract_scaling_history(
        self,
        table_name: str,
        days: int = 30,
    ) -> pd.DataFrame:
        """Extract scaling decisions from DynamoDB.

        Args:
            table_name: DynamoDB table name (default: k3s-scaling-history)
            days: Number of days to extract

        Returns:
            DataFrame with columns: timestamp, decision_id, action, cpu_percent,
                memory_percent, pending_pods, current_nodes, target_nodes, reason
        """
        table = self.dynamodb.Table(table_name)

        # Calculate start time
        start_time = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        print(f"Extracting scaling history from {table_name}...")

        records = []
        scan_kwargs = {
            "FilterExpression": "#ts >= :start_time",
            "ExpressionAttributeNames": {"#ts": "timestamp"},
            "ExpressionAttributeValues": {":start_time": start_time},
        }

        try:
            response = table.scan(**scan_kwargs)
            records.extend(response.get("Items", []))

            while "LastEvaluatedKey" in response:
                scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = table.scan(**scan_kwargs)
                records.extend(response.get("Items", []))

        except Exception as e:
            print(f"Error scanning table: {e}")
            return pd.DataFrame()

        if not records:
            print(f"No records found in {table_name}")
            return pd.DataFrame()

        # Convert to DataFrame
        df = pd.DataFrame(records)

        # Parse nested fields
        if "metrics" in df.columns:
            metrics_df = pd.json_normalize(df["metrics"].tolist())
            df = pd.concat([df.drop(columns=["metrics"]), metrics_df], axis=1)

        if "decision" in df.columns:
            decision_df = pd.json_normalize(df["decision"].tolist())
            df = pd.concat([df.drop(columns=["decision"]), decision_df], axis=1)

        # Parse timestamp
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])

        # Sort by timestamp
        df = df.sort_values("timestamp").reset_index(drop=True)

        print(f"Extracted {len(df)} scaling decisions")

        return df

    def save_to_csv(self, df: pd.DataFrame, output_path: str) -> None:
        """Save DataFrame to CSV file.

        Args:
            df: DataFrame to save
            output_path: Output file path
        """
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        df.to_csv(output_path, index=False)
        print(f"Saved {len(df)} records to {output_path}")


def main():
    """Main entry point for data extraction."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract data from DynamoDB for ML training"
    )
    parser.add_argument(
        "--profile",
        type=str,
        default="k3s-temp-user",
        help="AWS profile name",
    )
    parser.add_argument(
        "--region",
        type=str,
        default="ap-southeast-1",
        help="AWS region",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days to extract",
    )
    parser.add_argument(
        "--metrics-table",
        type=str,
        default="k3s-scaling-metrics-samples",
        help="Metrics samples table name",
    )
    parser.add_argument(
        "--history-table",
        type=str,
        default="k3s-scaling-history",
        help="Scaling history table name",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="ml_training/data",
        help="Output directory for CSV files",
    )

    args = parser.parse_args()

    # Create extractor
    extractor = DynamoDBExtractor(region=args.region, profile=args.profile)

    # Extract metrics samples
    print("=" * 60)
    print("Extracting Metrics Samples")
    print("=" * 60)
    metrics_df = extractor.extract_metrics_samples(
        table_name=args.metrics_table,
        days=args.days,
    )

    if not metrics_df.empty:
        extractor.save_to_csv(
            metrics_df,
            f"{args.output_dir}/metrics_samples.csv",
        )

        # Print summary statistics
        print("\nSummary Statistics:")
        print(metrics_df.describe())

    # Extract scaling history
    print("\n" + "=" * 60)
    print("Extracting Scaling History")
    print("=" * 60)
    history_df = extractor.extract_scaling_history(
        table_name=args.history_table,
        days=args.days,
    )

    if not history_df.empty:
        extractor.save_to_csv(
            history_df,
            f"{args.output_dir}/scaling_history.csv",
        )

        # Print action distribution
        print("\nScaling Actions:")
        print(history_df["action"].value_counts())

    print("\n" + "=" * 60)
    print("Data extraction complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
