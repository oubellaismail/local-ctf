#!/usr/bin/env python3
"""
admin_api.py: the dashboard's backend (controller-admin, port 5001).

Only CTFd's plugin talks to it, for logged-in CTFd admins, with X-Admin-Key.
Runs as a single process: deploy jobs run one at a time in a worker thread.

  GET    /health
  GET    /challenges                    manifests + CTFd state + instance counts
  PUT    /challenges/{id}               create/update a manifest
  POST   /challenges/{id}/upload        raw body: image .tar or files .zip
  POST   /challenges/{id}/deploy        → job  (?replace=1 recreates instances)
  POST   /challenges/{id}/visibility    {"state": "hidden"|"visible"}
  DELETE /challenges/{id}               → job
  POST   /deploy-all                    → job
  GET    /jobs, /jobs/{job_id}
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import manifests as mf
import pipeline
import registry
import static_build
from ctfd_api import CTFdError
from jobs import JobRunner
from k8s_client import init_k8s_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("admin_api")

# One line per HTTP request drowns the deploy log
logging.getLogger("httpx").setLevel(logging.WARNING)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
MAX_TAR_BYTES = int(os.environ.get("MAX_IMAGE_MB", "1024")) * 1024 * 1024
MAX_ZIP_BYTES = 50 * 1024 * 1024

def _cleanup_partials() -> None:
    """Half-written uploads and zip builds from a crash/restart."""
    up = pipeline.DATA_DIR / "uploads"
    if up.is_dir():
        for p in up.rglob("*.part"):
            p.unlink(missing_ok=True)
    static = pipeline.DATA_DIR / "static"
    if static.is_dir():
        for p in static.glob(".*.new"):
            shutil.rmtree(p, ignore_errors=True)


@asynccontextmanager
async def lifespan(_app):
    _cleanup_partials()
    yield


app = FastAPI(title="local-ctf admin", lifespan=lifespan)
runner = JobRunner()


@app.exception_handler(CTFdError)
def _ctfd_error(request: Request, exc: CTFdError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})
_ctx: Optional[pipeline.Context] = None


def ctx() -> pipeline.Context:
    global _ctx
    if _ctx is None:
        _ctx = pipeline.Context(*init_k8s_client())
    return _ctx


def require_admin(x_admin_key: str = Header("")) -> None:
    if not ADMIN_KEY or not secrets.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(status_code=403, detail="bad admin key")


def check_id(cid: str) -> str:
    if not mf.ID_RE.match(cid):
        raise HTTPException(status_code=400, detail="invalid challenge id")
    return cid


@app.get("/health")
def health():
    return {"ok": True, "busy": runner.busy()}


@app.get("/challenges", dependencies=[Depends(require_admin)])
def list_challenges():
    c = ctx()
    manifests = c.store.all()

    ctfd_by_name: dict = {}
    ctfd_error = None
    try:
        ctfd_by_name = pipeline.ctfd().challenges()
    except Exception as e:  # dashboard still lists manifests without CTFd
        ctfd_error = str(e)

    counts: dict[str, dict] = {}
    for d in c.apps_v1.list_namespaced_deployment(
            "challenges", label_selector="managed-by=ctf-controller").items:
        cid = (d.metadata.labels or {}).get("challenge", "")
        e = counts.setdefault(cid, {"instances": 0, "running": 0})
        e["instances"] += 1
        if (d.spec.replicas or 0) > 0:
            e["running"] += 1

    rows = []
    for cid, m in manifests.items():
        ch = ctfd_by_name.get(m.get("name"))
        rows.append({
            "manifest": m,
            "ctfd": {"id": ch["id"], "state": ch.get("state"), "value": ch.get("value")} if ch else None,
            **counts.get(cid, {"instances": 0, "running": 0}),
            "pending_image": pipeline.pending_image(cid).exists(),
            "has_files": pipeline.static_src(cid).is_dir() or pipeline.static_zip_count(cid) > 0,
            "zips": pipeline.static_zip_count(cid),
        })
    return {"challenges": rows, "ctfd_error": ctfd_error, "jobs": runner.recent(5)}


@app.put("/challenges/{cid}", dependencies=[Depends(require_admin)])
async def save_challenge(cid: str, request: Request):
    check_id(cid)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    body = {k: v for k, v in body.items() if v not in (None, "")}
    body["id"] = cid
    m = mf.normalize(body)
    errors = mf.validate_one(m, strict_fields=True)
    others = {k: v for k, v in ctx().store.all().items() if k != cid}
    if any(o.get("name") == m.get("name") for o in others.values()):
        errors.append(f"another challenge is already called '{m.get('name')}'")
    old = ctx().store.get(cid)
    if old and old.get("type") != m.get("type"):
        errors.append("type can't change after creation; delete and re-create the challenge")
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    ctx().store.put(m)
    return {"ok": True, "manifest": m, "created": old is None}


async def _receive(request: Request, dest: Path, limit: int) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(dest, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"file too large (max {limit // (1024 * 1024)} MB)")
            f.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="empty upload")
    return size


@app.post("/challenges/{cid}/upload", dependencies=[Depends(require_admin)])
async def upload(cid: str, request: Request):
    check_id(cid)
    m = ctx().store.get(cid)
    if not m:
        raise HTTPException(status_code=404, detail="save the challenge before uploading files")
    if runner.busy():
        raise HTTPException(status_code=409, detail="a deploy is running; upload when it has finished")
    if m["type"] in mf.SERVICE_TYPES:
        part = pipeline.uploads_dir(cid) / "image.tar.part"
        size = await _receive(request, part, MAX_TAR_BYTES)
        try:
            registry.check_image_tar(part)
        except registry.RegistryError as e:
            part.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=str(e))
        part.rename(pipeline.pending_image(cid))
        return {"ok": True, "kind": "image", "mb": round(size / 1048576, 1),
                "next": "Deploy pushes it to the registry"}
    part = pipeline.uploads_dir(cid) / "files.zip.part"
    await _receive(request, part, MAX_ZIP_BYTES)
    try:
        n = static_build.safe_extract(part, pipeline.static_src(cid))
    except static_build.StaticBuildError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        part.unlink(missing_ok=True)
    return {"ok": True, "kind": "files", "files": n, "next": "Deploy builds one zip per team"}


@app.post("/challenges/{cid}/deploy", dependencies=[Depends(require_admin)])
def deploy_one(cid: str, replace: int = 0):
    check_id(cid)
    if not ctx().store.get(cid):
        raise HTTPException(status_code=404, detail="unknown challenge")
    job = runner.submit(f"Deploy {cid}", lambda j: pipeline.deploy(
        ctx(), j, ids=[cid], state_for_new="hidden", replace_instances=bool(replace)))
    return {"job": job.id}


@app.post("/deploy-all", dependencies=[Depends(require_admin)])
def deploy_all():
    job = runner.submit("Deploy all challenges",
                        lambda j: pipeline.deploy(ctx(), j, ids=None, state_for_new="hidden"))
    return {"job": job.id}


@app.post("/challenges/{cid}/visibility", dependencies=[Depends(require_admin)])
async def visibility(cid: str, request: Request):
    check_id(cid)
    body = await request.json()
    state = (body or {}).get("state")
    if state not in ("hidden", "visible"):
        raise HTTPException(status_code=400, detail="state must be hidden or visible")
    m = ctx().store.get(cid)
    if not m:
        raise HTTPException(status_code=404, detail="unknown challenge")
    client = pipeline.ctfd()
    ch = client.challenges().get(m["name"])
    if not ch:
        raise HTTPException(status_code=409, detail="not in CTFd yet: deploy it first")
    client.set_state(ch["id"], state)
    return {"ok": True, "state": state}


@app.delete("/challenges/{cid}", dependencies=[Depends(require_admin)])
def delete_challenge(cid: str):
    check_id(cid)
    job = runner.submit(f"Delete {cid}", lambda j: pipeline.delete(ctx(), j, cid))
    return {"job": job.id}


@app.get("/jobs", dependencies=[Depends(require_admin)])
def jobs():
    return {"jobs": runner.recent(20)}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_admin)])
def job(job_id: str):
    j = runner.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="unknown job (controller-admin restarted?)")
    return j.to_dict()
