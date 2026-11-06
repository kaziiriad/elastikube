"""Shared test setup: put src/ on the path and isolate AWS/config state."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    # Never let tests reach real AWS or LocalStack.
    for var in ("AWS_ENDPOINT_URL", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-1")
    monkeypatch.setenv("CLUSTER_NAME", "test-cluster")
    monkeypatch.setenv("STATE_TABLE_NAME", "test-state")

    from utils.config import reset_config

    reset_config()
    yield
    reset_config()
