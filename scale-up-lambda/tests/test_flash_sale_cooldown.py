"""Scale-up cooldown gate vs. flash-sale events."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402


@pytest.fixture
def gates(monkeypatch):
    calls = {"cooldown": 0}

    def cooldown(*_a, **_k):
        calls["cooldown"] += 1
        return True, "Recent scale-up 30s ago"

    monkeypatch.setattr(main, "_get_config", lambda: {"state_table_name": "t", "cluster_name": "c"})
    monkeypatch.setattr(main, "_check_bootstrap_credentials", lambda _c: (True, ""))
    monkeypatch.setattr(main, "_check_recent_scale_up", cooldown)
    # Stop right after the cooldown gate: report another launch in progress.
    monkeypatch.setattr(main, "_check_scaling_in_progress", lambda *_a: (True, "Scaling already in progress"))
    return calls


def test_regular_scale_up_is_blocked_by_cooldown(gates):
    resp = main._handle_scale_up({"current_nodes": 3, "target_nodes": 4})

    assert json.loads(resp["body"])["reason"] == "Recent scale-up 30s ago"
    assert gates["cooldown"] == 1


def test_flash_sale_bypasses_cooldown_but_not_in_progress(gates):
    resp = main._handle_scale_up({"current_nodes": 3, "target_nodes": 4, "flash_sale": True})

    assert gates["cooldown"] == 0
    # Still honours scaling_in_progress, so no concurrent launches.
    assert json.loads(resp["body"])["reason"] == "Scaling already in progress"
