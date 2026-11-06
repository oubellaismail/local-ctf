"""
API endpoint tests using FastAPI TestClient with mocked K8s.

Tests all business logic without a live cluster:
  - Auth enforcement (401 on bad key)
  - 4-pod cap (429)
  - ChallengeNotFound (404)
  - /activate sets replicas=1 and returns URL
  - /deactivate clears annotation and returns suspended
  - /status returns empty list for unknown team
  - /health returns 200 with no auth
"""

import os
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException


# ─── Helpers to build fake K8s objects ──────────────────────────────────────

def _make_deploy(name, team_id, challenge_id, replicas=0, activated_at="", ready_replicas=None):
    d = MagicMock()
    d.metadata.name = name
    d.metadata.labels = {"team": team_id, "challenge": challenge_id, "managed-by": "ctf-controller"}
    d.metadata.annotations = {"ctf/activated-at": activated_at}
    d.spec.replicas = replicas
    d.status.ready_replicas = replicas if ready_replicas is None and replicas == 1 else (ready_replicas or 0)
    return d


def _make_apps_v1(deploys=None):
    """Return a mock AppsV1Api with controllable deployment list."""
    apps_v1 = MagicMock()
    deploy_list = MagicMock()
    deploy_list.items = deploys or []
    apps_v1.list_namespaced_deployment.return_value = deploy_list
    # read_namespaced_deployment raises 404 by default; tests override as needed
    apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
    apps_v1.patch_namespaced_deployment.return_value = MagicMock()
    return apps_v1


def _make_core_v1():
    core_v1 = MagicMock()
    cm = MagicMock()
    cm.data = {}
    core_v1.read_namespaced_config_map.return_value = cm
    svc = MagicMock()
    svc.spec.ports = [MagicMock(node_port=30080)]
    core_v1.read_namespaced_service.return_value = svc
    return core_v1


# ─── Test client fixture ─────────────────────────────────────────────────────

KEY = "testkey"

@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("CTF_MASTER_SECRET", "test-secret")
    monkeypatch.setenv("CONTROLLER_KEY", KEY)
    monkeypatch.setenv("CHALLENGE_HOST", "10.0.0.5")


def make_client(apps_v1=None, core_v1=None):
    """Build TestClient with patched K8s clients and lifespan."""
    apps_v1 = apps_v1 or _make_apps_v1()
    core_v1 = core_v1 or _make_core_v1()

    # Patch init_k8s_client so lifespan doesn't need a real cluster
    with patch("api.init_k8s_client", return_value=(apps_v1, core_v1, MagicMock())):
        # Import fresh to pick up the patched k8s client
        import importlib, api as api_module
        importlib.reload(api_module)
        with patch("api.init_k8s_client", return_value=(apps_v1, core_v1, MagicMock())):
            client = TestClient(api_module.app, raise_server_exceptions=False)
            # Inject mocked clients directly into app state
            api_module.app.state.apps_v1 = apps_v1
            api_module.app.state.core_v1 = core_v1
            return client, api_module


# ─── /health ────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_no_auth(self):
        client, _ = make_client()
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_health_ignores_bad_key(self):
        client, _ = make_client()
        r = client.get("/health", headers={"X-Controller-Key": "wrong"})
        assert r.status_code == 200


# ─── Auth enforcement ────────────────────────────────────────────────────────

class TestAuth:
    def test_activate_no_key_returns_401_or_422(self):
        client, _ = make_client()
        r = client.post("/activate", json={"team_id": "1", "challenge_id": "web-sqli"})
        assert r.status_code in (401, 422)

    def test_activate_wrong_key_returns_401(self):
        client, _ = make_client()
        r = client.post(
            "/activate",
            json={"team_id": "1", "challenge_id": "web-sqli"},
            headers={"X-Controller-Key": "wrongkey"},
        )
        assert r.status_code == 401
        assert "detail" in r.json()

    def test_status_wrong_key_returns_401(self):
        client, _ = make_client()
        r = client.get("/status/1", headers={"X-Controller-Key": "wrongkey"})
        assert r.status_code == 401


# ─── /status ────────────────────────────────────────────────────────────────

class TestStatus:
    def test_unknown_team_returns_empty_list(self):
        apps_v1 = _make_apps_v1(deploys=[])
        client, _ = make_client(apps_v1=apps_v1)
        r = client.get("/status/999", headers={"X-Controller-Key": KEY})
        assert r.status_code == 200
        assert r.json() == {"challenges": []}

    def test_fresh_challenge_status(self):
        deploy = _make_deploy("team-1-web-sqli", "1", "web-sqli", replicas=0, activated_at="")
        apps_v1 = _make_apps_v1(deploys=[deploy])
        client, _ = make_client(apps_v1=apps_v1)
        r = client.get("/status/1", headers={"X-Controller-Key": KEY})
        assert r.status_code == 200
        challenges = r.json()["challenges"]
        assert len(challenges) == 1
        assert challenges[0]["challenge_id"] == "web-sqli"
        assert challenges[0]["status"] == "fresh"
        assert challenges[0]["url"] is None

    def test_running_challenge_status(self):
        deploy = _make_deploy("team-1-web-sqli", "1", "web-sqli", replicas=1,
                              activated_at="2026-03-25T10:00:00+00:00")
        apps_v1 = _make_apps_v1(deploys=[deploy])
        client, _ = make_client(apps_v1=apps_v1)
        r = client.get("/status/1", headers={"X-Controller-Key": KEY})
        assert r.status_code == 200
        ch = r.json()["challenges"][0]
        assert ch["status"] == "running"
        assert ch["activated_at"] == "2026-03-25T10:00:00+00:00"

    def test_suspended_challenge_status(self):
        deploy = _make_deploy("team-1-web-sqli", "1", "web-sqli", replicas=0,
                              activated_at="2026-03-25T09:00:00+00:00")
        apps_v1 = _make_apps_v1(deploys=[deploy])
        client, _ = make_client(apps_v1=apps_v1)
        r = client.get("/status/1", headers={"X-Controller-Key": KEY})
        assert r.status_code == 200
        assert r.json()["challenges"][0]["status"] == "suspended"


# ─── /activate ───────────────────────────────────────────────────────────────

class TestActivate:
    def test_challenge_not_found_returns_404(self):
        apps_v1 = _make_apps_v1(deploys=[])  # 0 active, but read raises 404
        apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
        client, _ = make_client(apps_v1=apps_v1)
        r = client.post(
            "/activate",
            json={"team_id": "1", "challenge_id": "nonexistent"},
            headers={"X-Controller-Key": KEY},
        )
        assert r.status_code == 404
        assert "detail" in r.json()

    def test_four_pod_cap_returns_429(self):
        # 4 already-running deploys
        deploys = [
            _make_deploy(f"team-1-test-{i}", "1", f"test-{i}", replicas=1)
            for i in range(4)
        ]
        apps_v1 = _make_apps_v1(deploys=deploys)
        client, _ = make_client(apps_v1=apps_v1)
        r = client.post(
            "/activate",
            json={"team_id": "1", "challenge_id": "web-sqli"},
            headers={"X-Controller-Key": KEY},
        )
        assert r.status_code == 429
        assert "Max 4" in r.json()["detail"]

    def test_activate_success_http_challenge(self):
        apps_v1 = _make_apps_v1(deploys=[])
        # read_namespaced_deployment succeeds (deployment exists)
        apps_v1.read_namespaced_deployment.side_effect = None
        apps_v1.read_namespaced_deployment.return_value = MagicMock()
        client, api_module = make_client(apps_v1=apps_v1)
        api_module._manifest_cache = {"haaland": {"id": "haaland", "type": "http", "port": 8080}}
        r = client.post(
            "/activate",
            json={"team_id": "3", "challenge_id": "haaland"},
            headers={"X-Controller-Key": KEY},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "starting"
        # HTTP challenge should get a NodePort URL
        assert body["url"] == "http://10.0.0.5:30080"
        # Verify patch was called with replicas=1
        call_args = apps_v1.patch_namespaced_deployment.call_args
        patch_body = call_args[0][2]
        assert patch_body["spec"]["replicas"] == 1
        assert "ctf/activated-at" in patch_body["metadata"]["annotations"]

    def test_cap_with_three_running_allows_activate(self):
        # 3 running → still under cap
        deploys = [
            _make_deploy(f"team-2-test-{i}", "2", f"test-{i}", replicas=1)
            for i in range(3)
        ]
        apps_v1 = _make_apps_v1(deploys=deploys)
        apps_v1.read_namespaced_deployment.side_effect = None
        apps_v1.read_namespaced_deployment.return_value = MagicMock()
        client, _ = make_client(apps_v1=apps_v1)
        r = client.post(
            "/activate",
            json={"team_id": "2", "challenge_id": "web-sqli"},
            headers={"X-Controller-Key": KEY},
        )
        assert r.status_code == 200


# ─── /deactivate ─────────────────────────────────────────────────────────────

class TestDeactivate:
    def test_deactivate_success(self):
        deploy = _make_deploy("team-1-web-sqli", "1", "web-sqli", replicas=1,
                              activated_at="2026-03-25T10:00:00+00:00")
        apps_v1 = _make_apps_v1(deploys=[deploy])
        apps_v1.read_namespaced_deployment.side_effect = None
        apps_v1.read_namespaced_deployment.return_value = deploy
        client, _ = make_client(apps_v1=apps_v1)
        r = client.post(
            "/deactivate",
            json={"team_id": "1", "challenge_id": "web-sqli"},
            headers={"X-Controller-Key": KEY},
        )
        assert r.status_code == 200
        assert r.json() == {"status": "suspended"}
        # Verify replicas=0 was patched
        call_args = apps_v1.patch_namespaced_deployment.call_args
        patch_body = call_args[0][2]
        assert patch_body["spec"]["replicas"] == 0
        # annotation explicitly set to None so K8s strategic merge patch deletes it
        assert patch_body["metadata"]["annotations"]["ctf/activated-at"] is None

    def test_deactivate_not_found_returns_404(self):
        apps_v1 = _make_apps_v1(deploys=[])
        apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
        client, _ = make_client(apps_v1=apps_v1)
        r = client.post(
            "/deactivate",
            json={"team_id": "1", "challenge_id": "nonexistent"},
            headers={"X-Controller-Key": KEY},
        )
        assert r.status_code == 404
