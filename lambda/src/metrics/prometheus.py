"""Prometheus client for fetching K3s cluster metrics.

Queries Prometheus (running in K3s cluster via NodePort) for:
- CPU usage percentage
- Memory usage percentage
- Pending pod count
- Node availability
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from requests.exceptions import RequestException

from utils.config import get_config


@dataclass
class ClusterMetrics:
    """Metrics collected from the K3s cluster."""

    cpu_percent: float  # Average CPU usage across all nodes
    memory_percent: float  # Average memory usage across all nodes
    pending_pods: int  # Number of pods pending scheduling
    ready_nodes: int  # Number of Ready nodes
    total_nodes: int  # Total number of nodes
    timestamp: str  # ISO timestamp of metrics collection

    def to_dict(self) -> dict:
        """Convert to dict for CloudWatch metrics."""
        return {
            "cpu_percent": self.cpu_percent,
            "memory_percent": self.memory_percent,
            "pending_pods": self.pending_pods,
            "ready_nodes": self.ready_nodes,
            "total_nodes": self.total_nodes,
            "timestamp": self.timestamp,
        }


class PrometheusClient:
    """Client for querying Prometheus metrics."""

    def __init__(self, prometheus_url: Optional[str] = None, timeout: int = 10):
        """Initialize the Prometheus client.

        Args:
            prometheus_url: Prometheus URL (defaults to config)
            timeout: Request timeout in seconds
        """
        self._config = get_config()
        self._url = prometheus_url or self._config.prometheus_url
        self._timeout = timeout
        self._session = requests.Session()

    def query(self, promql: str) -> dict:
        """Execute a Prometheus PromQL query.

        Args:
            promql: PromQL query string

        Returns:
            Query response from Prometheus

        Raises:
            RuntimeError: If query fails
        """
        try:
            response = self._session.get(
                f"{self._url}/api/v1/query",
                params={"query": promql},
                timeout=self._timeout,
            )
            response.raise_for_status()

            data = response.json()
            if data.get("status") != "success":
                raise RuntimeError(f"Prometheus query failed: {data.get('error')}")

            return data.get("data", {})

        except RequestException as e:
            raise RuntimeError(f"Failed to query Prometheus: {e}") from e

    def get_cluster_metrics(self) -> ClusterMetrics:
        """Fetch all relevant cluster metrics.

        Returns:
            Cluster metrics including CPU, memory, and pod status

        Raises:
            RuntimeError: If metrics cannot be fetched
        """
        # Query CPU usage (average across all nodes) using node-exporter metrics
        # Matches Grafana dashboard query
        cpu_result = self.query("""
            avg(100 - (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
        """)

        cpu_percent = self._extract_average_value(cpu_result)

        # Query memory usage using node-exporter metrics
        # Matches Grafana dashboard query
        memory_result = self.query("""
            avg((1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)
        """)

        memory_percent = self._extract_average_value(memory_result)

        # Query pending pods - use SUM not COUNT
        # kube_pod_status_phase has value=1 for each pod, so we sum the values
        pending_result = self.query("""
            sum(kube_pod_status_phase{phase="Pending"})
        """)
        pending_pods = int(self._extract_value(pending_result) or 0)

        # Query node status - use kube_node_info for total nodes
        total_nodes_result = self.query("""
            count(kube_node_info)
        """)
        total_nodes = int(self._extract_value(total_nodes_result) or 0)

        # Query ready nodes - sum only true status
        ready_nodes_result = self.query("""
            sum(kube_node_status_condition{condition="Ready", status="true"})
        """)
        ready_nodes = int(self._extract_value(ready_nodes_result) or 0)

        return ClusterMetrics(
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            pending_pods=pending_pods,
            ready_nodes=ready_nodes,
            total_nodes=total_nodes,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _extract_value(query_result: dict) -> Optional[float]:
        """Extract scalar value from Prometheus query result.

        Args:
            query_result: Result from query()

        Returns:
            Float value or None if no data
        """
        result_type = query_result.get("resultType")
        results = query_result.get("result", [])

        if not results:
            return None

        if result_type == "vector":
            # For vector queries, return average of all values
            values = []
            for result in results:
                value = result.get("value", [])
                if len(value) >= 2:
                    try:
                        values.append(float(value[1]))
                    except (ValueError, IndexError):
                        pass
            return sum(values) / len(values) if values else None

        if result_type == "scalar":
            value = query_result.get("value", [])
            if len(value) >= 2:
                try:
                    return float(value[1])
                except (ValueError, IndexError):
                    pass

        return None

    @staticmethod
    def _extract_average_value(query_result: dict) -> float:
        """Extract average value from vector query result.

        Args:
            query_result: Result from query()

        Returns:
            Average float value, defaults to 0.0
        """
        value = PrometheusClient._extract_value(query_result)
        return value if value is not None else 0.0
