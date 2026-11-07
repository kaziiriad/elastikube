"""Boot-phase breakdown computed from EC2 LaunchTime + bootstrap tags."""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402

LAUNCH = datetime(2026, 11, 7, 9, 0, 0, tzinfo=timezone.utc)


def test_breakdown_splits_launch_boot_and_join():
    tags = {
        "BootstrapStartedAt": "2026-11-07T09:00:50Z",  # user-data started 50s after launch
        "BootUptimeAtStart": "35.40",                   # kernel had been up 35.4s by then
        "JoinedAt": "2026-11-07T09:00:56Z",             # joined 6s after user-data started
    }

    t = main._join_timing(LAUNCH, tags)

    assert t == {
        "launch_to_os_boot_s": 14.6,
        "os_boot_to_userdata_s": 35.4,
        "userdata_to_joined_s": 6.0,
        "launch_to_joined_s": 56.0,
    }


def test_missing_or_bad_tags_return_none():
    assert main._join_timing(LAUNCH, {"JoinedAt": "2026-11-07T09:00:56Z"}) is None
    assert main._join_timing(LAUNCH, {
        "BootstrapStartedAt": "not-a-date",
        "BootUptimeAtStart": "1",
        "JoinedAt": "2026-11-07T09:00:56Z",
    }) is None
