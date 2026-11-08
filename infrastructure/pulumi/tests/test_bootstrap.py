"""Master and worker user-data rendering (Phase 1, steps 2 and 3).

The master is configured by cloud-init from a script Pulumi renders, so the
rendered script has to be complete, valid bash, and must never carry the join
token itself (user-data is readable through DescribeInstanceAttribute).
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

PULUMI_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PULUMI_DIR))
import bootstrap  # noqa: E402

ARGS = dict(
    region="ap-southeast-1",
    secret_id="k3s-production-k3s-join-token",
    bucket="k3s-userdata-production-k3s",
    k3s_version="v1.37.2+k3s1",
    node_ip="10.0.2.10",
)


def test_every_placeholder_is_filled():
    script = bootstrap.render_master_user_data(**ARGS)

    assert script.startswith("#!/bin/bash")
    assert not re.search(r"__[A-Z0-9_]+__", script)
    for value in ARGS.values():
        assert value in script


def test_rendered_script_is_valid_bash(tmp_path):
    path = tmp_path / "user-data.sh"
    path.write_text(bootstrap.render_master_user_data(**ARGS))

    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_token_is_fetched_at_boot_not_embedded():
    script = bootstrap.render_master_user_data(**ARGS)

    assert "secretsmanager get-secret-value" in script
    assert 'K3S_TOKEN="$TOKEN"' in script


@pytest.mark.parametrize(
    "field,value",
    [
        ("region", 'ap-southeast-1"; rm -rf /'),
        ("secret_id", "name with spaces"),
        ("bucket", "bucket/$(id)"),
        ("k3s_version", "latest"),
        ("node_ip", "10.0.2"),
    ],
)
def test_unsafe_or_malformed_values_are_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        bootstrap.render_master_user_data(**{**ARGS, field: value})


def test_k3s_version_matches_ansible_pin():
    """Until Ansible leaves the bring-up path, both pins must agree."""
    group_vars = (PULUMI_DIR.parent / "ansible" / "group_vars" / "all.yml").read_text()
    ansible_pin = re.search(r"^k3s_install_version:\s*(\S+)", group_vars, re.M).group(1)

    assert bootstrap.DEFAULT_K3S_VERSION == ansible_pin


WORKER_ARGS = dict(
    cluster_name="production-k3s",
    region="ap-southeast-1",
    k3s_version="v1.37.2+k3s1",
)


def test_worker_script_is_complete_valid_bash(tmp_path):
    script = bootstrap.render_worker_user_data(**WORKER_ARGS)
    path = tmp_path / "worker.sh"
    path.write_text(script)

    assert script.startswith("#!/bin/bash")
    assert not re.search(r"__[A-Z0-9_]+__", script)
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_worker_installs_pinned_k3s_when_ami_has_none():
    script = bootstrap.render_worker_user_data(**WORKER_ARGS)

    assert "releases/download/v1.37.2%2Bk3s1/k3s" in script
    assert "[ ! -x /usr/local/bin/k3s ]" in script


def test_worker_never_logs_or_embeds_the_token():
    script = bootstrap.render_worker_user_data(**WORKER_ARGS)

    assert "secretsmanager get-secret-value" in script
    assert "JOIN_TOKEN:0" not in script


@pytest.mark.parametrize(
    "field,value",
    [("cluster_name", "prod k3s"), ("region", "nowhere"), ("k3s_version", "v1.37")],
)
def test_worker_rejects_malformed_values(field, value):
    with pytest.raises(ValueError, match=field):
        bootstrap.render_worker_user_data(**{**WORKER_ARGS, field: value})


def test_worker_agent_survives_reboot_under_systemd():
    """cloud-init runs user-data once, so the agent must be an enabled unit."""
    script = bootstrap.render_worker_user_data(**WORKER_ARGS)

    assert "/etc/systemd/system/k3s-agent.service" in script
    assert "Restart=always" in script
    assert "WantedBy=multi-user.target" in script
    assert "systemctl enable --now k3s-agent" in script


def test_worker_token_kept_out_of_world_readable_unit():
    script = bootstrap.render_worker_user_data(**WORKER_ARGS)
    unit = script.split("k3s-agent.service <<UNIT\n", 1)[1].split("\nUNIT\n", 1)[0]

    assert "EnvironmentFile=/etc/k3s-agent.env" in unit
    assert "JOIN_TOKEN" not in unit and "--token" not in unit
    assert "chmod 600 /etc/k3s-agent.env" in script
