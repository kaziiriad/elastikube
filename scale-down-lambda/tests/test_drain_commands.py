"""Node lookup + drain commands sent to the master via SSM."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402


def test_commands_resolve_node_by_exact_internal_ip():
    script = "\n".join(main._drain_commands("10.0.1.5"))

    # Node names carry a k3s node-id suffix, so never derive them from the IP.
    assert "ip-10.0.1.5" not in script
    # Exact InternalIP match: 10.0.1.5 must not also match 10.0.1.50.
    assert 'InternalIP' in script
    assert "$2 == ip" in script
    assert "-v ip='10.0.1.5'" in script


def test_commands_fail_when_node_missing_and_drain_before_delete():
    cmds = main._drain_commands("10.0.1.5")
    script = "\n".join(cmds)

    assert "exit 1" in script  # no node -> fail, so the caller aborts termination
    drain = next(i for i, c in enumerate(cmds) if "kubectl drain" in c)
    delete = next(i for i, c in enumerate(cmds) if "kubectl delete node" in c)
    assert drain < delete
    assert '"$NODE"' in cmds[drain] and '"$NODE"' in cmds[delete]
