#!/usr/bin/env python3
"""
api.py - FastAPI controller for team challenge activation/deactivation.

Endpoints:
  POST   /activate          - Activate a challenge for a team
  GET    /status/{team_id}  - Get status of all challenges for a team
  POST   /deactivate        - Deactivate a challenge for a team
  GET    /health            - Health check (no auth)
"""

import os
import secrets
import logging
import time
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from kubernetes.client.rest import ApiException

from k8s_client import init_k8s_client
from naming import deployment_name, service_name
from exceptions import ChallengeNotFound, MaxInstancesExceeded, ControllerKeyInvalid
from manifest_loader import load_manifests

logger = logging.getLogger(__name__)

# Host players use to reach challenge NodePorts (laptop LAN IP or campus hostname).
CHALLENGE_HOST = os.getenv("CHALLENGE_HOST", "")
# Max simultaneously running instances per team.
MAX_ACTIVE = int(os.getenv("MAX_ACTIVE_PER_TEAM", "4"))

# Cache manifests in memory for URL building
_manifest_cache: Optional[dict] = None

# Status cache: team_id → (result_dict, expiry_timestamp)
# TTL of 5s reduces K8s API calls under poll-heavy load.
_status_cache: dict = {}
_status_cache_lock = threading.Lock()
_STATUS_CACHE_TTL = 5.0  # seconds


def _get_status_cache(team_id: str):
    with _status_cache_lock:
        entry = _status_cache.get(team_id)
        if entry and time.monotonic() < entry[1]:
            return entry[0]
    return None


def _set_status_cache(team_id: str, result: dict):
    with _status_cache_lock:
        _status_cache[team_id] = (result, time.monotonic() + _STATUS_CACHE_TTL)


def _invalidate_status_cache(team_id: str):
    with _status_cache_lock:
        _status_cache.pop(team_id, None)


def get_challenge_manifests() -> dict:
    """Load and cache challenge manifests."""
    global _manifest_cache
    if _manifest_cache is None:
        _manifest_cache = {c["id"]: c for c in load_manifests()}
    return _manifest_cache


# Exception handlers
def challenge_not_found_handler(request: Request, exc: ChallengeNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Challenge not found"})


def max_instances_handler(request: Request, exc: MaxInstancesExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"Max {MAX_ACTIVE} active instances reached. Stop one to continue."},
    )


def controller_key_invalid_handler(request: Request, exc: ControllerKeyInvalid) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "Invalid controller key"})


# Lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize K8s clients and validate environment."""
    if not os.getenv("CTF_MASTER_SECRET"):
        raise ValueError("CTF_MASTER_SECRET env var not set")
    if not os.getenv("CONTROLLER_KEY"):
        raise ValueError("CONTROLLER_KEY env var not set")
    if not CHALLENGE_HOST:
        raise ValueError("CHALLENGE_HOST env var not set")

    apps_v1, core_v1, _ = init_k8s_client()
    app.state.apps_v1 = apps_v1
    app.state.core_v1 = core_v1

    logger.info("Controller API started")
    yield
    logger.info("Controller API shutdown")


# Create FastAPI app
app = FastAPI(lifespan=lifespan)

# Register exception handlers
app.add_exception_handler(ChallengeNotFound, challenge_not_found_handler)
app.add_exception_handler(MaxInstancesExceeded, max_instances_handler)
app.add_exception_handler(ControllerKeyInvalid, controller_key_invalid_handler)


# Request/Response models
class ActivateRequest(BaseModel):
    team_id: str
    challenge_id: str


class DeactivateRequest(BaseModel):
    team_id: str
    challenge_id: str


class ActivateResponse(BaseModel):
    status: str
    url: Optional[str] = None
    extra_ports: Optional[dict] = None


class DeactivateResponse(BaseModel):
    status: str


class ChallengeStatus(BaseModel):
    challenge_id: str
    status: str
    url: Optional[str] = None
    extra_ports: Optional[dict] = None
    activated_at: Optional[str] = None


class StatusResponse(BaseModel):
    challenges: list[ChallengeStatus]


# Auth dependency
def verify_controller_key(x_controller_key: str = Header(...)) -> None:
    """Verify X-Controller-Key header matches CONTROLLER_KEY env var."""
    expected = os.getenv("CONTROLLER_KEY", "")
    if not secrets.compare_digest(x_controller_key.encode(), expected.encode()):
        raise ControllerKeyInvalid()


def clear_activation_annotation(apps_v1: Any, dep_name: str) -> None:
    """
    Clear the ctf/activated-at annotation and set replicas to 0.

    Sets annotation value to null (not just omits it) so K8s strategic merge
    patch actually removes the key rather than leaving it unchanged.
    Idempotent: safe to call multiple times.
    """
    patch_body = {
        "metadata": {"annotations": {"ctf/activated-at": None}},
        "spec": {"replicas": 0},
    }
    apps_v1.patch_namespaced_deployment(dep_name, "challenges", patch_body)


def build_challenge_url(core_v1: Any, team_id: str, challenge_id: str) -> tuple[Optional[str], Optional[dict]]:
    """
    Build the challenge URL and extra ports for a team.

    For HTTP challenges: http://{CHALLENGE_HOST}:{port}
    For TCP challenges: nc {CHALLENGE_HOST} {port}
    Both use NodePort. Returns (None, None) if port not yet assigned.

    extra_ports: dict mapping port name → connection string for each additional
    exposed port (e.g. {"modbus": "192.168.1.50:32189"})
    """
    manifests = get_challenge_manifests()
    challenge = manifests.get(challenge_id)

    if not challenge:
        logger.warning(f"Manifest not found for challenge_id={challenge_id}")
        return None, None

    try:
        svc_name = service_name(team_id, challenge_id)
        svc = core_v1.read_namespaced_service(svc_name, "challenges")

        node_port = svc.spec.ports[0].node_port
        if not node_port:
            logger.warning(f"NodePort not assigned yet for {svc_name}")
            return None, None

        if challenge.get("type") == "http":
            url = f"http://{CHALLENGE_HOST}:{node_port}"
        else:
            url = f"nc {CHALLENGE_HOST} {node_port}"

        # Collect extra ports (any port beyond the first)
        extra_ports = {}
        for p in svc.spec.ports[1:]:
            name = p.name or str(p.port)
            extra_ports[name] = f"{CHALLENGE_HOST}:{p.node_port}"

        return url, extra_ports or None
    except ApiException as e:
        logger.error(f"Failed to build URL for {svc_name}: {e.status} {e.reason}")
        return None, None


@app.post("/activate", response_model=ActivateResponse)
def activate(req: ActivateRequest, request: Request, _=Depends(verify_controller_key)):
    """Activate a challenge for a team."""
    apps_v1 = request.app.state.apps_v1
    core_v1 = request.app.state.core_v1

    # Count active pods for this team — always fetch fresh from K8s (never use cache here,
    # a stale read could allow a team to exceed the instance cap under concurrent requests).
    deploys = apps_v1.list_namespaced_deployment(
        "challenges", label_selector=f"team={req.team_id},managed-by=ctf-controller"
    ).items
    active_count = sum(1 for d in deploys if d.spec.replicas == 1)

    # Check per-team instance cap
    if active_count >= MAX_ACTIVE:
        raise MaxInstancesExceeded()

    # Resolve deployment name and verify it exists
    dep_name = deployment_name(req.team_id, req.challenge_id)
    try:
        apps_v1.read_namespaced_deployment(dep_name, "challenges")
    except ApiException as e:
        if e.status == 404:
            raise ChallengeNotFound()
        raise

    # Build patch: set replicas=1 and record activation timestamp
    patch_body = {
        "spec": {"replicas": 1},
        "metadata": {"annotations": {"ctf/activated-at": datetime.now(timezone.utc).isoformat()}},
    }
    apps_v1.patch_namespaced_deployment(dep_name, "challenges", patch_body)

    url, extra_ports = build_challenge_url(core_v1, req.team_id, req.challenge_id)
    _invalidate_status_cache(req.team_id)
    logger.info(f"Activated {dep_name}")
    return {"status": "starting", "url": url, "extra_ports": extra_ports}


@app.post("/deactivate", response_model=DeactivateResponse)
def deactivate(req: DeactivateRequest, request: Request, _=Depends(verify_controller_key)):
    """Deactivate a challenge for a team."""
    apps_v1 = request.app.state.apps_v1

    dep_name = deployment_name(req.team_id, req.challenge_id)
    try:
        apps_v1.read_namespaced_deployment(dep_name, "challenges")
    except ApiException as e:
        if e.status == 404:
            raise ChallengeNotFound()
        raise

    clear_activation_annotation(apps_v1, dep_name)
    _invalidate_status_cache(req.team_id)
    logger.info(f"Deactivated {dep_name}")
    return {"status": "suspended"}


@app.get("/status/{team_id}", response_model=StatusResponse)
def get_status(team_id: str, request: Request, _=Depends(verify_controller_key)):
    """Get status of all challenges for a team. Returns empty list for unknown teams."""
    cached = _get_status_cache(team_id)
    if cached is not None:
        return cached

    apps_v1 = request.app.state.apps_v1
    core_v1 = request.app.state.core_v1

    deploys = apps_v1.list_namespaced_deployment(
        "challenges", label_selector=f"team={team_id},managed-by=ctf-controller"
    ).items

    if not deploys:
        return {"challenges": []}

    challenges = []
    for deploy in deploys:
        challenge_id = deploy.metadata.labels.get("challenge")
        desired_replicas = deploy.spec.replicas or 0
        ready_replicas = (deploy.status.ready_replicas or 0) if deploy.status else 0
        activated_at = (deploy.metadata.annotations or {}).get("ctf/activated-at", "")

        # Compute status — only "running" when pod is actually ready to serve
        if desired_replicas == 1 and ready_replicas == 1:
            status = "running"
        elif desired_replicas == 1 and ready_replicas == 0:
            status = "starting"
        elif desired_replicas == 0 and activated_at:
            status = "suspended"
        elif desired_replicas == 0 and not activated_at:
            status = "fresh"
        else:
            status = "unknown"

        url, extra_ports = build_challenge_url(core_v1, team_id, challenge_id) if status == "running" else (None, None)

        challenges.append(
            ChallengeStatus(
                challenge_id=challenge_id,
                status=status,
                url=url,
                extra_ports=extra_ports,
                activated_at=activated_at if activated_at else None,
            )
        )

    result = {"challenges": challenges}
    _set_status_cache(team_id, result)
    return result


@app.get("/health")
async def health_check():
    """Health check endpoint - no auth required."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=5000)
