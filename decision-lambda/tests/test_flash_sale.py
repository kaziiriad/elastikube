"""Flash sale (Layer 3) detection: stateless spike check + metrics + engine wiring."""

import json
from unittest.mock import MagicMock

import pytest

from metrics.prometheus import ClusterMetrics, PrometheusClient
from scaler.flash_sale import FlashSaleDetector
from scaler.scaling import ScalingAction, ScalingEngine
from state.cluster_state import ClusterState
from utils.config import reset_config


@pytest.fixture
def flash_enabled(monkeypatch):
    monkeypatch.setenv("FLASH_SALE_DETECTION_ENABLED", "true")
    monkeypatch.setenv("FLASH_SALE_CPU_SPIKE_THRESHOLD", "30")
    monkeypatch.setenv("FLASH_SALE_DETECTION_WINDOW_SECONDS", "120")
    reset_config()


def _metrics(cpu, window_min=None, workers=3):
    return ClusterMetrics(
        cpu_percent=cpu,
        memory_percent=40.0,
        pending_pods=0,
        ready_nodes=workers + 1,
        total_nodes=workers + 1,
        worker_count=workers,
        worker_cpu_percent_avg=cpu,
        worker_memory_percent_avg=40.0,
        master_cpu_percent=10.0,
        master_memory_percent=10.0,
        timestamp="2026-01-01T00:00:00+00:00",
        worker_cpu_window_min=window_min,
    )


# --- detector ---------------------------------------------------------------


def test_detects_spike_at_threshold(flash_enabled):
    assert FlashSaleDetector().detect(current_cpu=70.0, window_min_cpu=40.0) is True


def test_no_spike_below_threshold(flash_enabled):
    assert FlashSaleDetector().detect(current_cpu=69.0, window_min_cpu=40.0) is False


def test_no_detection_without_window_history(flash_enabled):
    assert FlashSaleDetector().detect(current_cpu=95.0, window_min_cpu=None) is False


def test_disabled_never_detects():
    assert FlashSaleDetector().detect(current_cpu=95.0, window_min_cpu=5.0) is False


# --- prometheus window query ------------------------------------------------


def _vector(v):
    return {"resultType": "vector", "result": [{"value": [0, str(v)]}]}


def _fake_query(window_min_value):
    queries = []

    def query(promql):
        queries.append(promql)
        if "kube_node_role" in promql and "count" not in promql:
            return {"resultType": "vector", "result": [{"metric": {"node": "master"}}]}
        if "min_over_time" in promql:
            if isinstance(window_min_value, Exception):
                raise window_min_value
            return _vector(window_min_value)
        return _vector(50)

    return query, queries


def test_window_min_queried_when_enabled(flash_enabled):
    client = PrometheusClient(prometheus_url="http://prom")
    client.query, queries = _fake_query(window_min_value=22.5)

    metrics = client.get_cluster_metrics()

    assert metrics.worker_cpu_window_min == 22.5
    subqueries = [q for q in queries if "min_over_time" in q]
    assert len(subqueries) == 1
    assert "[120s:15s]" in subqueries[0]
    assert 'instance!~".*(master).*"' in subqueries[0]  # workers only


def test_window_min_not_queried_when_disabled():
    client = PrometheusClient(prometheus_url="http://prom")
    client.query, queries = _fake_query(window_min_value=22.5)

    metrics = client.get_cluster_metrics()

    assert metrics.worker_cpu_window_min is None
    assert not any("min_over_time" in q for q in queries)


def test_window_query_failure_does_not_fail_metrics(flash_enabled):
    client = PrometheusClient(prometheus_url="http://prom")
    client.query, _ = _fake_query(window_min_value=RuntimeError("subquery boom"))

    metrics = client.get_cluster_metrics()

    assert metrics.worker_cpu_window_min is None
    assert metrics.worker_cpu_percent_avg == 50


# --- scaling engine ---------------------------------------------------------


def _state_in_cooldown():
    from datetime import datetime, timezone

    return ClusterState(
        cluster_id="test-cluster",
        node_count=3,
        scaling_in_progress=False,
        last_scale_time=datetime.now(timezone.utc).isoformat(),
    )


def test_flash_sale_scales_up_despite_cooldown(flash_enabled, monkeypatch):
    monkeypatch.setattr("scaler.scaling.PREDICTIVE_AVAILABLE", False)
    decision = ScalingEngine().evaluate(_metrics(75.0, window_min=40.0), _state_in_cooldown())

    assert decision.action == ScalingAction.SCALE_UP
    assert decision.flash_sale is True
    assert decision.to_dict()["flash_sale"] is True


def test_no_flash_sale_respects_cooldown(flash_enabled, monkeypatch):
    monkeypatch.setattr("scaler.scaling.PREDICTIVE_AVAILABLE", False)
    decision = ScalingEngine().evaluate(_metrics(75.0, window_min=70.0), _state_in_cooldown())

    assert decision.action == ScalingAction.NO_OP
    assert decision.flash_sale is False


# --- event publication ------------------------------------------------------


def test_scale_up_event_carries_flash_sale_flag(flash_enabled, monkeypatch):
    import main

    monkeypatch.setattr("scaler.scaling.PREDICTIVE_AVAILABLE", False)
    decision = ScalingEngine().evaluate(_metrics(75.0, window_min=40.0), _state_in_cooldown())
    events = MagicMock()
    events.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "e1"}]}
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: events)

    main._publish_scale_up_event(decision)

    detail = json.loads(events.put_events.call_args.kwargs["Entries"][0]["Detail"])
    assert detail["flash_sale"] is True
