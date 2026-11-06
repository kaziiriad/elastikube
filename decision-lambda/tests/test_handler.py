"""Decision Lambda handler behaviour when Prometheus is unreachable."""

import json
from unittest.mock import MagicMock

import pytest

import main
from state.cluster_state import ClusterState


@pytest.fixture
def handler_deps(monkeypatch):
    state_manager = MagicMock()
    state_manager.get_state.return_value = ClusterState(
        cluster_id="test-cluster", node_count=3, scaling_in_progress=False
    )
    lock = MagicMock()
    lock.acquire.return_value = True
    wal = MagicMock()
    wal.get_incomplete_operations.return_value = []
    prometheus = MagicMock()
    prometheus.get_cluster_metrics.side_effect = RuntimeError("connection refused")
    engine = MagicMock()

    monkeypatch.setattr(main, "get_clients", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(main, "StateManager", lambda *_: state_manager)
    monkeypatch.setattr(main, "DistributedLock", lambda *_: lock)
    monkeypatch.setattr(main, "WriteAheadLog", lambda *_: wal)
    monkeypatch.setattr(main, "PrometheusClient", lambda *_: prometheus)
    monkeypatch.setattr(main, "ScalingEngine", lambda *_: engine)
    monkeypatch.setattr(main, "DynamoDBRecorder", lambda *_: MagicMock())
    publish_metrics = MagicMock()
    scale_up = MagicMock()
    scale_down = MagicMock()
    monkeypatch.setattr(main, "_publish_cloudwatch_metrics", publish_metrics)
    monkeypatch.setattr(main, "_publish_scale_up_event", scale_up)
    monkeypatch.setattr(main, "_publish_scale_down_event", scale_down)
    return {
        "engine": engine,
        "lock": lock,
        "state_manager": state_manager,
        "publish_metrics": publish_metrics,
        "scale_up": scale_up,
        "scale_down": scale_down,
    }


def test_prometheus_outage_does_not_scale(handler_deps):
    resp = main.lambda_handler({}, None)

    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["action"] == "NO_OP"
    handler_deps["scale_up"].assert_not_called()
    handler_deps["scale_down"].assert_not_called()
    handler_deps["engine"].evaluate.assert_not_called()
    handler_deps["lock"].release.assert_called_once()


def test_prometheus_outage_reports_unhealthy_without_fake_metrics(handler_deps):
    main.lambda_handler({}, None)

    handler_deps["publish_metrics"].assert_called_once()
    args, kwargs = handler_deps["publish_metrics"].call_args
    assert kwargs.get("prometheus_healthy") is False
    # No fabricated 100% CPU sample should be published.
    assert args[0] is None


def test_prometheus_outage_does_not_overwrite_node_count(handler_deps):
    main.lambda_handler({}, None)

    handler_deps["state_manager"].update_state.assert_not_called()
