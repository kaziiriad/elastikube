"""Multi-node scale-up: launch N back to back, verify all in one loop."""

import json
import os
import sys
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402


# --- launch count -----------------------------------------------------------


@pytest.mark.parametrize(
    "detail,expected",
    [
        ({"current_nodes": 3, "target_nodes": 6}, 3),
        ({"current_nodes": 3, "target_nodes": 4}, 1),
        ({"current_nodes": 3, "target_nodes": 9}, 3),   # capped by MAX_SCALE_UP_STEP
        ({"current_nodes": 3, "target_nodes": 3}, 1),   # never below 1
        ({"current_nodes": 3}, 1),                      # missing target
        ({"current_nodes": "x", "target_nodes": 5}, 1), # malformed
    ],
)
def test_launch_count(monkeypatch, detail, expected):
    monkeypatch.setenv("MAX_SCALE_UP_STEP", "3")
    assert main._launch_count(detail) == expected


# --- handler ----------------------------------------------------------------


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("MAX_SCALE_UP_STEP", "3")
    monkeypatch.setattr(main, "_get_config", lambda: {
        "state_table_name": "t", "cluster_name": "c", "bootstrap_timeout": 180,
    })
    monkeypatch.setattr(main, "_check_bootstrap_credentials", lambda _c: (True, ""))
    monkeypatch.setattr(main, "_check_recent_scale_up", lambda *_a, **_k: (False, ""))
    monkeypatch.setattr(main, "_check_scaling_in_progress", lambda *_a: (False, ""))
    monkeypatch.setattr(main, "_get_pending_instances", lambda *_a, **_k: [])
    monkeypatch.setattr(main, "_acquire_distributed_lock", lambda *_a, **_k: (True, "lock"))
    monkeypatch.setattr(main, "_release_distributed_lock", lambda *_a: None)
    monkeypatch.setattr(main, "_set_scaling_in_progress", lambda *_a: None)
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: MagicMock())

    launches = []

    def launch():
        n = len(launches) + 1
        launches.append(n)
        return {"statusCode": 200, "body": json.dumps({
            "instance_id": f"i-{n}", "config": {"subnet": f"s-{n}", "availability_zone": "az"},
        })}

    verify = MagicMock(side_effect=lambda ids, timeout_seconds: {
        i: {"success": True, "private_ip": "10.0.0.1", "api_status": "ready"} for i in ids
    })
    monkeypatch.setattr(main, "_launch_test_instance", launch)
    monkeypatch.setattr(main, "_verify_nodes_joined", verify)
    return launches, verify


def test_launches_n_and_verifies_once(handler):
    launches, verify = handler

    resp = main._handle_scale_up({"current_nodes": 3, "target_nodes": 6})

    body = json.loads(resp["body"])
    assert len(launches) == 3
    verify.assert_called_once()
    assert verify.call_args.args[0] == ["i-1", "i-2", "i-3"]
    assert body["instance_ids"] == ["i-1", "i-2", "i-3"]
    assert body["joined"] == 3


def test_partial_launch_failure_verifies_what_launched(handler, monkeypatch):
    launches, verify = handler
    ok = main._launch_test_instance

    def flaky():
        if len(launches) == 1:
            launches.append("fail")
            return {"statusCode": 500, "body": json.dumps({"error": "InsufficientInstanceCapacity"})}
        return ok()

    monkeypatch.setattr(main, "_launch_test_instance", flaky)

    resp = main._handle_scale_up({"current_nodes": 3, "target_nodes": 5})

    body = json.loads(resp["body"])
    assert verify.call_args.args[0] == ["i-1"]
    assert body["instance_ids"] == ["i-1"]
    assert body["requested"] == 2


# --- verification loop ------------------------------------------------------


@pytest.fixture
def ec2():
    with mock_aws():
        client = boto3.client("ec2", region_name="ap-southeast-1")
        yield client


def _instance(client, tags):
    image = client.describe_images()["Images"][0]["ImageId"]
    iid = client.run_instances(ImageId=image, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    if tags:
        client.create_tags(Resources=[iid], Tags=[{"Key": k, "Value": v} for k, v in tags.items()])
    return iid


def test_verify_returns_each_instance_status(ec2, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-1")
    joined = _instance(ec2, {"JoinStatus": "success"})
    failed = _instance(ec2, {"JoinStatus": "failed"})

    results = main._verify_nodes_joined([joined, failed], timeout_seconds=5)

    assert results[joined]["success"] is True
    assert results[failed]["success"] is False
    assert results[failed]["api_status"] == "failed"


def test_verify_times_out_unresolved_instances(ec2, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-1")
    pending = _instance(ec2, {})

    results = main._verify_nodes_joined([pending], timeout_seconds=0)

    assert results[pending]["success"] is False
    assert results[pending]["api_status"] == "timeout"
