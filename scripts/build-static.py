#!/usr/bin/env python3
"""
build-static.py — build per-team zips for "static" (file download) challenges.

Every team gets its own flag (same HMAC formula as the controller), so a
leaked file only works for the team that downloaded it.

For each manifest with type: static, the source is challenges/static/<id>/:

  Option A — templated files (simplest)
    Put the challenge files there. Every text file has {{FLAG}} replaced with
    the team's flag. At least one file must contain {{FLAG}}.

  Option B — build script (for binaries, images, pcaps, ...)
    Add build.sh. It runs once per team with:
      FLAG     the team's flag
      TEAM_ID  CTFd team id
      OUT_DIR  empty directory: put the files players should receive there
    Working directory is the challenge folder.

Output: build/static/<id>/<team_id>.zip, served by the static-files pod.

Env: CTFD_URL, CTFD_ADMIN_TOKEN, CTF_MASTER_SECRET, FLAG_PREFIX
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "controller"))
from flag import derive_flag  # noqa: E402  (same formula the CTFd plugin checks)

MANIFESTS = ROOT / "challenges" / "manifests"
SRC = ROOT / "challenges" / "static"
OUT = ROOT / "build" / "static"
PLACEHOLDER = "{{FLAG}}"


def fetch_team_ids() -> list[str]:
    url = os.environ["CTFD_URL"].rstrip("/")
    s = requests.Session()
    s.headers.update({"Authorization": f"Token {os.environ['CTFD_ADMIN_TOKEN']}",
                      "Content-Type": "application/json"})
    ids: list[str] = []
    page = 1
    while True:
        r = s.get(f"{url}/api/v1/teams?per_page=100&page={page}", timeout=15)
        r.raise_for_status()
        body = r.json()
        ids += [str(t["id"]) for t in body.get("data", [])]
        nxt = (body.get("meta") or {}).get("pagination", {}).get("next")
        if not nxt:
            return ids
        page = nxt


def zip_dir(directory: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(directory.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(directory))
    return buf.getvalue()


def build_templated(src: Path, flag: str, work: Path) -> bool:
    found = False
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        dest = work / rel
        if p.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = p.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            shutil.copyfile(p, dest)
            continue
        if PLACEHOLDER in text:
            found = True
            text = text.replace(PLACEHOLDER, flag)
        dest.write_text(text, encoding="utf-8")
    return found


def build_one(cid: str, src: Path, team_id: str) -> bytes:
    flag = derive_flag(team_id, cid)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "out"
        work.mkdir()
        if (src / "build.sh").exists():
            env = {**os.environ, "FLAG": flag, "TEAM_ID": team_id, "OUT_DIR": str(work)}
            res = subprocess.run(["bash", "build.sh"], cwd=src, env=env,
                                 capture_output=True, text=True)
            if res.returncode != 0:
                sys.exit(f"  ✘ {cid}: build.sh failed for team {team_id}:\n{res.stderr[-2000:]}")
            if not any(work.iterdir()):
                sys.exit(f"  ✘ {cid}: build.sh wrote nothing to $OUT_DIR")
        elif not build_templated(src, flag, work):
            sys.exit(f"  ✘ {cid}: no file contains {PLACEHOLDER} and there is no build.sh "
                     "— players would get a file without their flag")
        return zip_dir(work)


def main() -> int:
    for var in ("CTFD_URL", "CTFD_ADMIN_TOKEN", "CTF_MASTER_SECRET"):
        if not os.environ.get(var):
            sys.exit(f"{var} not set")

    statics = []
    for f in sorted(MANIFESTS.glob("*.yaml")):
        if f.name == "schema.yaml":
            continue
        m = yaml.safe_load(f.read_text()) or {}
        if m.get("type") == "static":
            statics.append(m["id"])
    if not statics:
        print("  no static challenges")
        return 0

    teams = fetch_team_ids()
    if not teams:
        sys.exit("  ✘ no teams in CTFd yet — import teams first")

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    (OUT / ".keep").touch()
    for cid in statics:
        src = SRC / cid
        if not src.is_dir():
            sys.exit(f"  ✘ {cid}: missing challenges/static/{cid}/")
        (OUT / cid).mkdir()
        for team_id in teams:
            (OUT / cid / f"{team_id}.zip").write_bytes(build_one(cid, src, team_id))
        print(f"  built {cid}: {len(teams)} team zips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
