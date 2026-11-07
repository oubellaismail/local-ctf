#!/usr/bin/env python3
"""
build-static.py: build per-team zips for the download ("static") challenges
in challenges/manifests, from challenges/static/<id>/.

  - text files containing {{FLAG}} get each team's flag, or
  - build.sh runs once per team with FLAG, TEAM_ID and OUT_DIR set
    (for binaries, images, pcaps...)

Output: build/static/<id>/<team_id>.zip. deploy-challenges.sh copies them
to the platform-data volume, where static-files serves them.

Same builder as the dashboard (controller/static_build.py), which only
supports the {{FLAG}} form for uploads.

Env: CTFD_URL, CTFD_ADMIN_TOKEN, CTF_MASTER_SECRET, FLAG_PREFIX
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "controller"))
from flag import derive_flag  # noqa: E402  (same formula the CTFd plugin checks)
import static_build  # noqa: E402

MANIFESTS = ROOT / "challenges" / "manifests"
SRC = ROOT / "challenges" / "static"
OUT = ROOT / "build" / "static"


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

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    if not statics:
        print("  no download challenges in challenges/manifests")
        return 0

    teams = fetch_team_ids()
    if not teams:
        sys.exit("  ✘ no teams in CTFd yet — import teams first")

    for cid in statics:
        try:
            n = static_build.build_challenge(cid, SRC / cid, OUT, teams, derive_flag, allow_build_sh=True)
        except static_build.StaticBuildError as e:
            sys.exit(f"  ✘ {e}")
        print(f"  built {cid}: {n} team zips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
