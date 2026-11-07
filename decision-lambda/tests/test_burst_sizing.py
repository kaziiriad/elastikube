"""Scale-up size from Pending pods' CPU requests (instead of always +1)."""

import pytest

from metrics.prometheus import ClusterMetrics, PrometheusClient
from scaler.scaling import ScalingAction, ScalingEngine
from state.cluster_state import ClusterState
from utils.config import reset_config


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setenv("MIN_NODES", "1")
    monkeypatch.setenv("MAX_NODES", "10")
    monkeypatch.setenv("MAX_SCALE_UP_STEP", "3")
    monkeypatch.setattr("scaler.scaling.PREDICTIVE_AVAILABLE", False)
    reset_config()


def _metrics(workers, pending, pending_cpu=None, allocatable=None):
    return ClusterMetrics(
        cpu_percent=40.0,
        memory_percent=40.0,
        pending_pods=pending,
        ready_nodes=workers + 1,
        total_nodes=workers + 1,
        worker_count=workers,
        worker_cpu_percent_avg=40.0,
        worker_memory_percent_avg=40.0,
        master_cpu_percent=10.0,
        master_memory_percent=10.0,
        timestamp="2026-01-01T00:00:00+00:00",
        pending_cpu_cores=pending_cpu,
        worker_allocatable_cores=allocatable,
    )


def _state():
    return ClusterState(cluster_id="test-cluster", node_count=3, scaling_in_progress=False)


@pytest.mark.parametrize(
    "workers,pending_cpu,allocatable,expected_target",
    [
        (3, 5.0, 2.0, 6),    # ceil(5/2) = 3 nodes
        (3, 2.5, 2.0, 5),    # ceil(2.5/2) = 2 nodes
        (3, 0.1, 2.0, 4),    # tiny request still needs a node
        (3, 20.0, 2.0, 6),   # capped at MAX_SCALE_UP_STEP = 3
        (9, 8.0, 2.0, 10),   # capped at MAX_NODES = 10
    ],
)
def test_pending_pods_scale_by_requested_cpu(workers, pending_cpu, allocatable, expected_target):
    decision = ScalingEngine().evaluate(
        _metrics(workers, pending=4, pending_cpu=pending_cpu, allocatable=allocatable), _state()
    )

    assert decision.action == ScalingAction.SCALE_UP
    assert decision.target_nodes == expected_target


@pytest.mark.parametrize("pending_cpu,allocatable", [(None, 2.0), (5.0, None), (5.0, 0.0), (0.0, 2.0)])
def test_falls_back_to_one_node_without_usable_metrics(pending_cpu, allocatable):
    decision = ScalingEngine().evaluate(
        _metrics(3, pending=2, pending_cpu=pending_cpu, allocatable=allocatable), _state()
    )

    assert decision.action == ScalingAction.SCALE_UP
    assert decision.target_nodes == 4


# --- prometheus -------------------------------------------------------------


def _vector(v):
    return {"resultType": "vector", "result": [{"value": [0, str(v)]}]}


def test_prometheus_reports_pending_cpu_and_allocatable():
    queries = []

    def query(promql):
        queries.append(promql)
        if "kube_node_role" in promql and "count" not in promql and "unless" not in promql:
            return {"resultType": "vector", "result": [{"metric": {"node": "master"}}]}
        if "kube_pod_container_resource_requests" in promql:
            return _vector(3.5)
        if "kube_node_status_allocatable" in promql:
            return _vector(2.0)
        return _vector(50)

    client = PrometheusClient(prometheus_url="http://prom")
    client.query = query
    m = client.get_cluster_metrics()

    assert m.pending_cpu_cores == 3.5
    assert m.worker_allocatable_cores == 2.0
    pending_q = next(q for q in queries if "kube_pod_container_resource_requests" in q)
    assert 'phase="Pending"' in pending_q
    alloc_q = next(q for q in queries if "kube_node_status_allocatable" in q)
    assert 'role="control-plane"' in alloc_q  # workers only


def test_sizing_query_failures_do_not_fail_metrics():
    def query(promql):
        if "kube_pod_container_resource_requests" in promql or "kube_node_status_allocatable" in promql:
            raise RuntimeError("boom")
        if "kube_node_role" in promql and "count" not in promql:
            return {"resultType": "vector", "result": []}
        return _vector(50)

    client = PrometheusClient(prometheus_url="http://prom")
    client.query = query
    m = client.get_cluster_metrics()

    assert m.pending_cpu_cores is None
    assert m.worker_allocatable_cores is None
    assert m.worker_cpu_percent_avg == 50
