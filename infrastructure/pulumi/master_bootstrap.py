"""Render the k3s master's cloud-init user-data.

Kept free of Pulumi imports so it can be unit-tested; __main__.py feeds it
resolved values through Output.apply.
"""

import re
from pathlib import Path

# Must match k3s_install_version in infrastructure/ansible/group_vars/all.yml
# while Ansible still installs the permanent workers (enforced by a test).
DEFAULT_K3S_VERSION = "v1.37.2+k3s1"

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "master-user-data.sh"

# Values are substituted into a bash script, so each one must match a strict
# pattern: anything else is a configuration error, not something to escape.
_PATTERNS: dict[str, str] = {
    "region": r"[a-z]{2}(-[a-z]+)+-\d",
    "secret_id": r"[A-Za-z0-9/_+=.@-]{1,512}",
    "bucket": r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
    "k3s_version": r"v\d+\.\d+\.\d+\+k3s\d+",
    "node_ip": r"(\d{1,3}\.){3}\d{1,3}",
}


def render_master_user_data(
    *, region: str, secret_id: str, bucket: str, k3s_version: str, node_ip: str
) -> str:
    """Return the master bootstrap script with every placeholder filled.

    Raises ValueError naming the first field that is not a safe, well-formed value.
    """
    values = {
        "region": region,
        "secret_id": secret_id,
        "bucket": bucket,
        "k3s_version": k3s_version,
        "node_ip": node_ip,
    }
    for field, value in values.items():
        if not re.fullmatch(_PATTERNS[field], value):
            raise ValueError(f"{field} is not a valid value for the master bootstrap: {value!r}")

    script = TEMPLATE_PATH.read_text()
    for field, value in values.items():
        script = script.replace(f"__{field.upper()}__", value)
    return script
