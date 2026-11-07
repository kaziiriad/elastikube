"""Workers launch from the Pulumi-managed Launch Template.

The template carries the AMI, instance type, security group, instance profile,
key pair and bootstrap user-data, so the Lambda passes only what varies per
launch: the subnet, tags, and (when a baked AMI is registered) an AMI override.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402

BASE_CONFIG = {
    "subnet_ids": ["s-1", "s-2"], "subnet_id": "s-1", "launch_template_id": "lt-0abc",
    "ami_id": None, "state_table_name": None, "cluster_name": "c",
    "use_spot_instances": False, "bootstrap_timeout": 180,
}


def _launch(monkeypatch, config):
    ec2 = MagicMock()
    ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-1", "PrivateIpAddress": "10.0.2.4"}]}
    monkeypatch.setattr(main.boto3, "client", lambda svc, *a, **k: ec2 if svc == "ec2" else MagicMock())
    monkeypatch.setattr(main, "_get_config", lambda: config)
    monkeypatch.setattr(main, "_get_next_subnet_id", lambda cfg: ("s-2", "ap-southeast-1b"))

    resp = main._launch_test_instance()

    assert resp["statusCode"] == 200, json.loads(resp["body"])
    return ec2.run_instances.call_args.kwargs


def test_launches_from_template_with_subnet_override(monkeypatch):
    params = _launch(monkeypatch, BASE_CONFIG)

    assert params["LaunchTemplate"] == {"LaunchTemplateId": "lt-0abc", "Version": "$Latest"}
    assert params["SubnetId"] == "s-2"
    for owned_by_template in ("UserData", "SecurityGroupIds", "IamInstanceProfile", "InstanceType", "KeyName"):
        assert owned_by_template not in params
    tags = {t["Key"]: t["Value"] for t in params["TagSpecifications"][0]["Tags"]}
    assert tags["NodeRole"] == "worker" and tags["Permanent"] == "false"


def test_template_ami_used_unless_a_baked_ami_is_registered(monkeypatch):
    assert "ImageId" not in _launch(monkeypatch, BASE_CONFIG)

    params = _launch(monkeypatch, {**BASE_CONFIG, "ami_id": "ami-baked"})

    assert params["ImageId"] == "ami-baked"


def test_config_requires_launch_template_not_bootstrap_bucket(monkeypatch):
    for var in ("USER_DATA_S3_BUCKET", "USER_DATA_S3_KEY", "AMI_ID", "SECURITY_GROUP_ID",
                "IAM_INSTANCE_PROFILE", "INSTANCE_TYPE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SUBNET_IDS", '["s-1"]')
    monkeypatch.setenv("SUBNET_ID", "s-1")
    monkeypatch.setattr(main, "_get_worker_ami_id", MagicMock(side_effect=RuntimeError("no param")))

    monkeypatch.delenv("LAUNCH_TEMPLATE_ID", raising=False)
    with pytest.raises(RuntimeError, match="launch_template_id"):
        main._get_config()

    monkeypatch.setenv("LAUNCH_TEMPLATE_ID", "lt-0abc")
    config = main._get_config()

    assert config["launch_template_id"] == "lt-0abc"
    assert config["ami_id"] is None
