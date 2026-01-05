"""Prometheus metrics fetching for K3s autoscaler."""

from .prometheus import PrometheusClient, ClusterMetrics

__all__ = ["PrometheusClient", "ClusterMetrics"]
