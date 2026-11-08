"""Deploy pipeline shared by the dashboard (controller-admin) and the CLI.

    deploy()  manifests (ConfigMap) → registry → CTFd → controller-api reload
              → per-team instances → team flags → per-team download zips
    delete()  remove a challenge everywhere

Paths under DATA_DIR (the platform-data volume):
    uploads/<id>/image.tar   image uploaded from the dashboard, until pushed
    static-src/<id>/         download-challenge files uploaded from the dashboard
    static/<id>/<team>.zip   per-team zips, served read-only by static-files
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bulk_spawn
import import_flags
import manifests as mf
import registry
import static_build
from ctfd_api import CTFd
from flag import derive_flag
from jobs import Job

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CTFD_URL = os.environ.get("CTFD_URL", "http://ctfd-service.ctfd.svc.cluster.local")
TOKEN_FILE = Path(os.environ.get("CTFD_TOKEN_FILE", "/secrets/ctfd/CTFD_ADMIN_TOKEN"))
NAMESPACE = "ctfd"


def uploads_dir(cid: str) -> Path:
    return DATA_DIR / "uploads" / cid


def pending_image(cid: str) -> Path:
    return uploads_dir(cid) / "image.tar"


def static_src(cid: str) -> Path:
    return DATA_DIR / "static-src" / cid


def static_out() -> Path:
    return DATA_DIR / "static"


def static_zip_count(cid: str) -> int:
    d = static_out() / cid
    return sum(1 for _ in d.glob("*.zip")) if d.is_dir() else 0


def admin_token() -> str:
    """CTFD_ADMIN_TOKEN env (passed by `make challenges`, always current),
    else the mounted secret, read on every use because make ctfd-setup or a
    restore can change it while controller-admin keeps running."""
    tok = os.environ.get("CTFD_ADMIN_TOKEN", "").strip()
    if tok:
        return tok
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def ctfd() -> CTFd:
    return CTFd(CTFD_URL, admin_token())


class Context:
    def __init__(self, apps_v1, core_v1, net_v1=None):
        self.apps_v1 = apps_v1
        self.core_v1 = core_v1
        self.clients = (apps_v1, core_v1, net_v1)
        self.store = mf.ManifestStore(core_v1)


def restart_controller_api(ctx: Context, job: Job, timeout: int = 180) -> None:
    """controller-api caches manifests at startup: restart it so it knows
    about new/changed challenges (ports, URLs)."""
    stamp = datetime.now(timezone.utc).isoformat()
    ctx.apps_v1.patch_namespaced_deployment("controller-api", NAMESPACE, {
        "spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}
    })
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = ctx.apps_v1.read_namespaced_deployment("controller-api", NAMESPACE)
        st, want = d.status, d.spec.replicas or 1
        if ((st.observed_generation or 0) >= d.metadata.generation
                and (st.updated_replicas or 0) >= want
                and (st.available_replicas or 0) >= want
                and (st.replicas or 0) == want):
            job.log("controller-api reloaded")
            return
        time.sleep(2)
    raise RuntimeError("controller-api didn't come back within 3 minutes "
                       "(kubectl -n ctfd get pods -l app=controller-api)")


def delete_instances(ctx: Context, cid: str, job: Job) -> int:
    """Delete all team deployments/services/secrets of one challenge."""
    sel = f"challenge={cid},managed-by=ctf-controller"
    n = 0
    for d in ctx.apps_v1.list_namespaced_deployment("challenges", label_selector=sel).items:
        ctx.apps_v1.delete_namespaced_deployment(d.metadata.name, "challenges")
        n += 1
    for s in ctx.core_v1.list_namespaced_service("challenges", label_selector=sel).items:
        ctx.core_v1.delete_namespaced_service(s.metadata.name, "challenges")
    for s in ctx.core_v1.list_namespaced_secret("challenges", label_selector=sel).items:
        ctx.core_v1.delete_namespaced_secret(s.metadata.name, "challenges")
    if n:
        job.log(f"removed {n} existing {cid} instance(s)")
    return n


NO_FILES = "has no files yet"


def _has_files(cid: str) -> bool:
    return static_src(cid).is_dir() or static_zip_count(cid) > 0


def deploy(ctx: Context, job: Job, ids: Optional[list[str]] = None,
           state_for_new: str = "hidden", replace_instances: bool = False) -> None:
    """Deploy the given challenges (default: all of them).

    replace_instances: recreate existing team instances (running ones stop),
    needed after changing a challenge's image, port or memory.
    """
    with job.step("Check manifests") as st:
        everything = ctx.store.all()
        if ids:
            missing = [i for i in ids if i not in everything]
            if missing:
                raise RuntimeError(f"unknown challenge(s): {', '.join(missing)}")
            chosen = [everything[i] for i in ids]
        else:
            chosen = list(everything.values())
        if not chosen:
            raise RuntimeError("no challenges to deploy")
        errors = mf.validate_all(((m["id"], m) for m in everything.values()), _has_files)
        mine = [e for e in errors if e.split(":", 1)[0] in {m["id"] for m in chosen}]
        skipped = []
        if not ids:
            # "Deploy all": a download challenge without files (e.g. a repo
            # challenge after new-round: only `make challenges` can rebuild
            # its zips) shouldn't block every other challenge.
            no_files = {e.split(":", 1)[0] for e in mine if NO_FILES in e}
            other = [e for e in mine if NO_FILES not in e]
            if no_files and not other:
                skipped = sorted(no_files)
                chosen = [m for m in chosen if m["id"] not in no_files]
                mine = []
                for cid in skipped:
                    job.log(f"! skipped {cid}: no files. Upload a .zip, or run `make challenges` "
                            "if it comes from challenges/static/")
        if mine:
            raise RuntimeError("; ".join(mine))
        if not chosen:
            raise RuntimeError("nothing to deploy: " + ", ".join(skipped) + " have no files")
        st["detail"] = f"{len(chosen)} challenge(s)" + (f", skipped {', '.join(skipped)} (no files)" if skipped else "")

    services = [m for m in chosen if m["type"] in mf.SERVICE_TYPES]
    statics = [m for m in chosen if m["type"] == "static"]

    pending = [m for m in services if pending_image(m["id"]).exists()]
    if pending:
        with job.step("Push images to the registry") as st:
            for m in pending:
                path = pending_image(m["id"])
                size = path.stat().st_size // (1024 * 1024)
                job.log(f"pushing {m['id']} ({size} MB)…")
                ref = registry.push_tar(path, m["id"])
                path.unlink()
                job.log(f"pushed {ref}")
            st["detail"] = f"{len(pending)} image(s)"

    client = ctfd()
    with job.step("Add to CTFd") as st:
        for m in chosen:
            action, cid = client.upsert_challenge(m, state_for_new=state_for_new)
            job.log(f"{action} {m['name']} (CTFd id {cid})")
        st["detail"] = f"{len(chosen)} challenge(s)"

    if services:
        with job.step("Reload controller"):
            restart_controller_api(ctx, job)

        with job.step("Create team instances") as st:
            if replace_instances:
                for m in services:
                    delete_instances(ctx, m["id"], job)
                time.sleep(3)  # let deletions settle before re-creating names
            res = bulk_spawn.run(client.url, admin_token(), manifests=services, clients=ctx.clients)
            if res["errors"]:
                raise RuntimeError(f"{res['errors']} instance(s) failed (see log)")
            st["detail"] = f"{res['created']} created, {res['skipped']} already there"

    with job.step("Register team flags") as st:
        res = import_flags.run(client.url, admin_token(), manifests=chosen)
        if res["errors"]:
            raise RuntimeError(f"{res['errors']} flag(s) failed (see log)")
        st["detail"] = f"{res['created']} registered"

    buildable = [m for m in statics if static_src(m["id"]).is_dir()]
    if buildable:
        with job.step("Build download files") as st:
            teams = client.team_ids()
            if not teams:
                raise RuntimeError("no teams in CTFd yet: import teams first")
            static_out().mkdir(parents=True, exist_ok=True)
            for m in buildable:
                n = static_build.build_challenge(m["id"], static_src(m["id"]), static_out(),
                                                 teams, derive_flag, allow_build_sh=False)
                job.log(f"built {m['id']}: {n} team zips")
            st["detail"] = f"{len(buildable)} challenge(s) × {len(teams)} teams"


def delete(ctx: Context, job: Job, cid: str) -> None:
    m = ctx.store.get(cid)
    with job.step("Remove team instances"):
        delete_instances(ctx, cid, job)
    with job.step("Remove from CTFd"):
        if m:
            client = ctfd()
            existing = client.challenges().get(m["name"])
            if existing:
                client.delete_challenge(existing["id"])
                job.log(f"deleted CTFd challenge {existing['id']}")
    with job.step("Remove files and manifest"):
        for d in (static_out() / cid, static_src(cid), uploads_dir(cid)):
            if d.exists():
                shutil.rmtree(d)
        ctx.store.delete(cid)
