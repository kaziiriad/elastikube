"""Tests for cooldown evaluation and the DynamoDB distributed lock."""

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from state.cluster_state import ClusterState, DistributedLock

TABLE = "test-state"
CLUSTER = "test-cluster"


def _state(last_scale_time):
    return ClusterState(
        cluster_id=CLUSTER,
        node_count=2,
        scaling_in_progress=False,
        last_scale_time=last_scale_time,
    )


# --- cooldown ---------------------------------------------------------------


def test_cooldown_active_for_tz_aware_timestamp():
    # Scale Lambdas write datetime.now(timezone.utc).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    assert _state(recent).is_in_cooldown(120) is True


def test_cooldown_active_for_naive_timestamp():
    recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).replace(tzinfo=None).isoformat()
    assert _state(recent).is_in_cooldown(120) is True


def test_cooldown_expired():
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    assert _state(old).is_in_cooldown(120) is False


def test_no_cooldown_without_timestamp():
    assert _state("").is_in_cooldown(120) is False


# --- distributed lock -------------------------------------------------------


@pytest.fixture
def ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-southeast-1")
        client.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "cluster_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cluster_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _put_flag(client, in_progress, since=None):
    item = {"cluster_id": {"S": CLUSTER}, "scaling_in_progress": {"S": in_progress}}
    if since is not None:
        item["scaling_in_progress_since"] = {"S": since}
    client.put_item(TableName=TABLE, Item=item)


def _item(client):
    return client.get_item(TableName=TABLE, Key={"cluster_id": {"S": CLUSTER}})["Item"]


def test_acquire_on_empty_table_and_release_clears_flag(ddb):
    lock = DistributedLock(ddb)
    assert lock.acquire(timeout_seconds=1) is True
    assert _item(ddb)["scaling_in_progress"]["S"] == "true"
    assert "scaling_in_progress_since" in _item(ddb)

    lock.release()
    item = _item(ddb)
    assert item["scaling_in_progress"]["S"] == "false"
    assert "scaling_in_progress_since" not in item
    assert "lock_owner" not in item


def test_fresh_lock_held_by_other_blocks(ddb):
    _put_flag(ddb, "true", since=datetime.now(timezone.utc).isoformat())
    assert DistributedLock(ddb).acquire(timeout_seconds=1) is False


def test_stale_lock_is_taken_over(ddb):
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    _put_flag(ddb, "true", since=stale)
    assert DistributedLock(ddb).acquire(timeout_seconds=1) is True


def test_stuck_legacy_lock_without_since_is_taken_over(ddb):
    # Old decision-lock writes set the flag without a timestamp; if the
    # Lambda died before release this wedged the autoscaler forever.
    _put_flag(ddb, "true")
    assert DistributedLock(ddb).acquire(timeout_seconds=1) is True


def test_release_does_not_clear_lock_taken_over_by_another_owner(ddb):
    first = DistributedLock(ddb)
    assert first.acquire(timeout_seconds=1) is True
    # Simulate another run taking over the lock (e.g. first one was judged stale).
    ddb.update_item(
        TableName=TABLE,
        Key={"cluster_id": {"S": CLUSTER}},
        UpdateExpression="SET lock_owner = :o",
        ExpressionAttributeValues={":o": {"S": "someone-else"}},
    )

    first.release()  # must not raise, must not clear the other owner's lock

    item = _item(ddb)
    assert item["scaling_in_progress"]["S"] == "true"
    assert item["lock_owner"]["S"] == "someone-else"
