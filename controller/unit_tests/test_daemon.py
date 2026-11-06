"""
Unit tests for daemon reconcile_once().
"""

import pytest
from unittest.mock import MagicMock, call
from datetime import datetime, timezone, timedelta


def _make_deploy(name, replicas=1, activated_at=None):
    d = MagicMock()
    d.metadata.name = name
    d.metadata.annotations = {"ctf/activated-at": activated_at or ""}
    d.spec.replicas = replicas
    return d


def make_apps_v1(deploys):
    apps_v1 = MagicMock()
    deploy_list = MagicMock()
    deploy_list.items = deploys
    apps_v1.list_namespaced_deployment.return_value = deploy_list
    return apps_v1


@pytest.fixture(autouse=True)
def controller_path():
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestReconcileOnce:
    def test_skips_inactive_deployments(self, monkeypatch):
        from daemon import reconcile_once
        # replicas=0, should be skipped even if activation timestamp is old
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        deploy = _make_deploy("team-1-web-sqli", replicas=0, activated_at=old_ts)
        apps_v1 = make_apps_v1([deploy])
        suspended = reconcile_once(apps_v1)
        assert suspended == 0
        apps_v1.patch_namespaced_deployment.assert_not_called()

    def test_skips_active_not_expired(self, monkeypatch):
        from daemon import reconcile_once
        # replicas=1, activated 30 minutes ago → not expired
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        deploy = _make_deploy("team-1-web-sqli", replicas=1, activated_at=recent_ts)
        apps_v1 = make_apps_v1([deploy])
        suspended = reconcile_once(apps_v1)
        assert suspended == 0
        apps_v1.patch_namespaced_deployment.assert_not_called()

    def test_suspends_expired_deployment(self, monkeypatch):
        from daemon import reconcile_once
        # replicas=1, activated 61 minutes ago → expired
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
        deploy = _make_deploy("team-1-web-sqli", replicas=1, activated_at=old_ts)
        apps_v1 = make_apps_v1([deploy])
        suspended = reconcile_once(apps_v1)
        assert suspended == 1
        # Verify patch set replicas=0 and cleared annotation
        call_args = apps_v1.patch_namespaced_deployment.call_args
        patch_body = call_args[0][2]
        assert patch_body["spec"]["replicas"] == 0
        # annotation must be explicitly set to None so K8s deletes the key
        assert patch_body["metadata"]["annotations"]["ctf/activated-at"] is None

    def test_skips_active_with_no_timestamp(self, monkeypatch):
        from daemon import reconcile_once
        # replicas=1 but no activation timestamp → skip (shouldn't happen but safe)
        deploy = _make_deploy("team-1-web-sqli", replicas=1, activated_at="")
        apps_v1 = make_apps_v1([deploy])
        suspended = reconcile_once(apps_v1)
        assert suspended == 0

    def test_suspends_only_expired_out_of_multiple(self, monkeypatch):
        from daemon import reconcile_once
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=65)).isoformat()
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        deploys = [
            _make_deploy("team-1-web-sqli", replicas=1, activated_at=old_ts),    # expired
            _make_deploy("team-2-web-sqli", replicas=1, activated_at=recent_ts), # fresh
            _make_deploy("team-3-pwn-bof",  replicas=0, activated_at=old_ts),    # inactive
        ]
        apps_v1 = make_apps_v1(deploys)
        suspended = reconcile_once(apps_v1)
        assert suspended == 1
        assert apps_v1.patch_namespaced_deployment.call_count == 1
        patched_name = apps_v1.patch_namespaced_deployment.call_args[0][0]
        assert patched_name == "team-1-web-sqli"

    def test_under_60_minutes_not_suspended(self, monkeypatch):
        from daemon import reconcile_once
        # 3595 seconds ago → safely under 3600s threshold (daemon uses > 3600, not >=)
        under_ts = (datetime.now(timezone.utc) - timedelta(seconds=3595)).isoformat()
        deploy = _make_deploy("team-1-web-sqli", replicas=1, activated_at=under_ts)
        apps_v1 = make_apps_v1([deploy])
        suspended = reconcile_once(apps_v1)
        assert suspended == 0
