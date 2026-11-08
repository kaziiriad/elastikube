"""Render cloud-init user-data for the k3s master and workers.

Kept free of Pulumi imports so it can be unit-tested; __main__.py feeds them
resolved values through Output.apply.
"""

import re
from pathlib import Path

# Must match k3s_install_version in infrastructure/ansible/group_vars/all.yml
# while Ansible still installs the permanent workers (enforced by a test).
DEFAULT_K3S_VERSION = "v1.37.2+k3s1"

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Values are substituted into a bash script, so each one must match a strict
# pattern: anything else is a configuration error, not something to escape.
_PATTERNS: dict[str, str] = {
    "region": r"[a-z]{2}(-[a-z]+)+-\d",
    "secret_id": r"[A-Za-z0-9/_+=.@-]{1,512}",
    "bucket": r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
    "k3s_version": r"v\d+\.\d+\.\d+\+k3s\d+",
    "node_ip": r"(\d{1,3}\.){3}\d{1,3}",
    "cluster_name": r"[a-z0-9][a-z0-9-]{0,62}",
}


def _render(template_name: str, values: dict[str, str]) -> str:
    """Validate every value, then fill __FIELD__ placeholders in the template.

    Raises ValueError naming the first field that is not a safe, well-formed value.
    """
    for field, value in values.items():
        if not re.fullmatch(_PATTERNS[field], value):
            raise ValueError(f"{field} is not a valid value for {template_name}: {value!r}")

    script = (TEMPLATES_DIR / template_name).read_text()
    for field, value in values.items():
        script = script.replace(f"__{field.upper()}__", value)
    return script


def render_master_user_data(
    *, region: str, secret_id: str, bucket: str, k3s_version: str, node_ip: str
) -> str:
    """Return the master bootstrap script with every placeholder filled."""
    return _render(
        "master-user-data.sh",
        {
            "region": region,
            "secret_id": secret_id,
            "bucket": bucket,
            "k3s_version": k3s_version,
            "node_ip": node_ip,
        },
    )


def render_worker_user_data(*, cluster_name: str, region: str, k3s_version: str) -> str:
    """Return the worker bootstrap script with every placeholder filled.

    The download URL needs the version with '+' percent-encoded; it is derived
    after validation so it can't carry anything the version pattern rejects.
    """
    script = _render(
        "worker-user-data.sh",
        {"cluster_name": cluster_name, "region": region, "k3s_version": k3s_version},
    )
    return script.replace("__K3S_VERSION_URL__", k3s_version.replace("+", "%2B"))
