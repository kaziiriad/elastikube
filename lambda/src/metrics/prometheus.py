"""Prometheus client for fetching K3s cluster metrics.

Queries Prometheus (running in K3s cluster via NodePort) for:
- CPU usage percentage (workers vs master separately)
- Memory usage percentage (workers vs master separately)
- Pending pod count
- Node availability
"""

import os
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List

import requests
from requests.exceptions import RequestException

from utils.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class ClusterMetrics:
    """Metrics collected from the K3s cluster."""

    cpu_percent: float  # Average CPU usage across WORKER nodes only (excludes master)
    memory_percent: float  # Average memory usage across WORKER nodes only (excludes master)
    pending_pods: int  # Number of pods pending scheduling
    ready_nodes: int  # Number of Ready nodes (all nodes)
    total_nodes: int  # Total number of nodes (all nodes)
    worker_count: int  # Number of worker nodes only
    master_cpu_percent: float  # Master node CPU usage
    master_memory_percent: float  # Master node memory usage
    timestamp: str  # ISO timestamp of metrics collection

    def to_dict(self) -> dict:
        """Convert to dict for CloudWatch metrics."""
        return {
            "cpu_percent": self.cpu_percent,
            "memory_percent": self.memory_percent,
            "pending_pods": self.pending_pods,
            "ready_nodes": self.ready_nodes,
            "total_nodes": self.total_nodes,
            "worker_count": self.worker_count,
            "master_cpu_percent": self.master_cpu_percent,
            "master_memory_percent": self.master_memory_percent,
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

        Uses kube_node_role to separate master/control-plane nodes from workers.
        """

        # ==========================================
        # MASTER/WORKER SEPARATION STRATEGY
        # ==========================================
        # kube_node_role has entries like: {node="master", role="control-plane"}
        # We use this to identify which nodes are master vs worker

        # Get list of control-plane node names
        control_plane_nodes_result = self.query('kube_node_role{role="control-plane"}')
        control_plane_nodes = [
            r.get("metric", {}).get("node", "")
            for r in control_plane_nodes_result.get("result", [])
        ]

        # ==========================================
        # WORKER NODE METRICS (exclude control-plane)
        # ==========================================
        worker_cpu_percent = 0.0
        worker_memory_percent = 0.0
        master_cpu_percent = 0.0
        master_memory_percent = 0.0

        if control_plane_nodes:
            # Build regex pattern to exclude control-plane nodes
            # Escape dots in node names for regex
            escaped_nodes = [n.replace(".", r"\.") for n in control_plane_nodes]
            exclude_pattern = "|".join(escaped_nodes)

            logger.info(f"Control-plane nodes found: {control_plane_nodes}")
            logger.info(f"Using exclude pattern: {exclude_pattern}")

            # Query WORKER CPU (exclude control-plane nodes, only K3s nodes)
            worker_cpu_result = self.query(f"""
                avg(
                    100 - (
                        avg by(instance) (
                            irate(node_cpu_seconds_total{{job="node-exporter-k3s-nodes",mode="idle",instance!~".*({exclude_pattern}).*"}}[5m])
                        ) * 100
                    )
                )
            """)
            worker_cpu_percent = self._extract_average_value(worker_cpu_result)

            # Query WORKER Memory
            worker_memory_result = self.query(f"""
                avg(
                    (1 - (
                        node_memory_MemAvailable_bytes{{job="node-exporter-k3s-nodes",instance!~".*({exclude_pattern}).*"}} /
                        node_memory_MemTotal_bytes{{job="node-exporter-k3s-nodes",instance!~".*({exclude_pattern}).*"}}
                    )) * 100
                )
            """)
            worker_memory_percent = self._extract_average_value(worker_memory_result)

            # Query MASTER CPU (only control-plane nodes)
            master_cpu_result = self.query(f"""
                avg(
                    100 - (
                        avg by(instance) (
                            irate(node_cpu_seconds_total{{job="node-exporter-k3s-nodes",mode="idle",instance=~".*({exclude_pattern}).*"}}[5m])
                        ) * 100
                    )
                )
            """)
            master_cpu_percent = self._extract_average_value(master_cpu_result)

            # Query MASTER Memory
            master_memory_result = self.query(f"""
                avg(
                    (1 - (
                        node_memory_MemAvailable_bytes{{job="node-exporter-k3s-nodes",instance=~".*({exclude_pattern}).*"}} /
                        node_memory_MemTotal_bytes{{job="node-exporter-k3s-nodes",instance=~".*({exclude_pattern}).*"}}
                    )) * 100
                )
            """)
            master_memory_percent = self._extract_average_value(master_memory_result)
        else:
            # No control-plane nodes found, use all K3s nodes as workers
            logger.warning("No control-plane nodes found via kube_node_role, using all K3s nodes as workers")
            worker_cpu_result = self.query("""
                avg(100 - (avg by(instance) (irate(node_cpu_seconds_total{job="node-exporter-k3s-nodes",mode="idle"}[5m])) * 100))
            """)
            worker_cpu_percent = self._extract_average_value(worker_cpu_result)

            worker_memory_result = self.query("""
                avg((1 - (node_memory_MemAvailable_bytes{job="node-exporter-k3s-nodes"} / node_memory_MemTotal_bytes{job="node-exporter-k3s-nodes"})) * 100)
            """)
            worker_memory_percent = self._extract_average_value(worker_memory_result)

        # ==========================================
        # CLUSTER-WIDE METRICS
        # ==========================================

        # Query pending pods
        pending_result = self.query("sum(kube_pod_status_phase{phase='Pending'})")
        pending_pods = int(self._extract_value(pending_result) or 0)

        # Query total nodes (all nodes including master)
        total_nodes_result = self.query("count(kube_node_info)")
        total_nodes = int(self._extract_value(total_nodes_result) or 0)

        # Query worker count (exclude control-plane nodes)
        worker_count_result = self.query("""
            count(kube_node_info) - count(kube_node_role{role="control-plane"})
        """)
        worker_count = int(self._extract_value(worker_count_result) or 0)

        # Query ready nodes - sum only true status
        ready_nodes_result = self.query("""
            sum(kube_node_status_condition{condition="Ready", status="true"})
        """)
        ready_nodes = int(self._extract_value(ready_nodes_result) or 0)

        return ClusterMetrics(
            cpu_percent=worker_cpu_percent,
            memory_percent=worker_memory_percent,
            pending_pods=pending_pods,
            ready_nodes=ready_nodes,
            total_nodes=total_nodes,
            worker_count=worker_count,
            master_cpu_percent=master_cpu_percent,
            master_memory_percent=master_memory_percent,
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