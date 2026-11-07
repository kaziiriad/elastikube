"""UserData must reach RunInstances as the plain script.

boto3 base64-encodes RunInstances UserData itself; encoding it beforehand
delivers base64 text to the instance instead of the bootstrap script.
"""

import base64
import json
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402

SCRIPT = "#!/bin/bash\necho bootstrap\n"


def test_run_instances_gets_plain_script(monkeypatch):
    ec2 = MagicMock()
    ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-1", "PrivateIpAddress": "10.0.2.4"}]}
    monkeypatch.setattr(main.boto3, "client", lambda svc, *a, **k: ec2 if svc == "ec2" else MagicMock())
    monkeypatch.setattr(main, "_get_config", lambda: {
        "subnet_ids": [], "subnet_id": "s-1", "security_group_id": "sg-1",
        "iam_instance_profile": "prof", "ami_id": "ami-1", "instance_type": "t3.small",
        "key_name": None, "s3_bucket": "b", "s3_key": "k", "state_table_name": None,
        "cluster_name": "c", "use_spot_instances": False, "bootstrap_timeout": 180,
    })
    monkeypatch.setattr(main, "_get_next_subnet_id", lambda cfg: ("s-1", "ap-southeast-1a"))
    monkeypatch.setattr(main, "_fetch_bootstrap_script", lambda *a: SCRIPT)

    resp = main._launch_test_instance()

    assert resp["statusCode"] == 200, json.loads(resp["body"])
    user_data = ec2.run_instances.call_args.kwargs["UserData"]
    assert user_data == SCRIPT
    assert user_data != base64.b64encode(SCRIPT.encode()).decode()
