"""Master user-data rendering (Phase 1, step 2).

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
import master_bootstrap  # noqa: E402

ARGS = dict(
    region="ap-southeast-1",
    secret_id="k3s-production-k3s-join-token",
    bucket="k3s-userdata-production-k3s",
    k3s_version="v1.37.2+k3s1",
    node_ip="10.0.2.10",
)


def test_every_placeholder_is_filled():
    script = master_bootstrap.render_master_user_data(**ARGS)

    assert script.startswith("#!/bin/bash")
    assert not re.search(r"__[A-Z0-9_]+__", script)
    for value in ARGS.values():
        assert value in script


def test_rendered_script_is_valid_bash(tmp_path):
    path = tmp_path / "user-data.sh"
    path.write_text(master_bootstrap.render_master_user_data(**ARGS))

    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_token_is_fetched_at_boot_not_embedded():
    script = master_bootstrap.render_master_user_data(**ARGS)

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
        master_bootstrap.render_master_user_data(**{**ARGS, field: value})


def test_k3s_version_matches_ansible_pin():
    """Until Ansible leaves the bring-up path, both pins must agree."""
    group_vars = (PULUMI_DIR.parent / "ansible" / "group_vars" / "all.yml").read_text()
    ansible_pin = re.search(r"^k3s_install_version:\s*(\S+)", group_vars, re.M).group(1)

    assert master_bootstrap.DEFAULT_K3S_VERSION == ansible_pin
