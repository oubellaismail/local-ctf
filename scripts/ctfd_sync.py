#!/usr/bin/env python3
"""
ctfd_sync.py — validate challenge manifests and create/update them in CTFd.

  validate   check every manifest in challenges/manifests (no network)
  sync       create missing CTFd challenges (dynamic scoring) and update the
             description/category/state of existing ones. Matched by name.

Env: CTFD_URL, CTFD_ADMIN_TOKEN (sync only), REQUESTS_CA_BUNDLE for our CA.
Run through scripts/deploy-challenges.sh rather than directly.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = ROOT / "challenges" / "manifests"
REGISTRY = "ctf-registry:5000/"
ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")


def load() -> list[tuple[Path, dict]]:
    out = []
    for f in sorted(MANIFESTS.glob("*.yaml")):
        if f.name == "schema.yaml":
            continue
        m = yaml.safe_load(f.read_text()) or {}
        out.append((f, m))
    return out


def validate(manifests: list[tuple[Path, dict]]) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for f, m in manifests:
        where = f.name
        cid = str(m.get("id", ""))
        if not ID_RE.match(cid):
            errors.append(f"{where}: id '{cid}' must be lowercase letters/digits/hyphens, max 40 chars "
                          "(it becomes part of Kubernetes names)")
        if cid in seen_ids:
            errors.append(f"{where}: duplicate id '{cid}'")
        seen_ids.add(cid)
        for field in ("name", "category", "type"):
            if not m.get(field):
                errors.append(f"{where}: missing '{field}'")
        if m.get("name") in seen_names:
            errors.append(f"{where}: duplicate name '{m.get('name')}' (CTFd matches challenges by name)")
        seen_names.add(m.get("name"))
        ctype = m.get("type")
        if ctype not in ("http", "tcp", "static"):
            errors.append(f"{where}: type must be http, tcp or static (got '{ctype}')")
        if ctype in ("http", "tcp"):
            if not isinstance(m.get("port"), int):
                errors.append(f"{where}: service challenges need an integer 'port'")
            image = str(m.get("image", ""))
            if not image.startswith(REGISTRY):
                errors.append(f"{where}: image must start with {REGISTRY} (push it with "
                              f"scripts/push-challenge.sh), got '{image}'")
        if ctype == "static":
            src = ROOT / "challenges" / "static" / cid
            if not src.is_dir():
                errors.append(f"{where}: static challenge needs files in challenges/static/{cid}/")
        if "flags" in m:
            for i, fl in enumerate(m["flags"]):
                for field in ("id", "name", "env"):
                    if not fl.get(field):
                        errors.append(f"{where}: flags[{i}] missing '{field}'")
    return errors


def sync(manifests: list[tuple[Path, dict]]) -> None:
    url = os.environ["CTFD_URL"].rstrip("/")
    token = os.environ["CTFD_ADMIN_TOKEN"]
    state = os.environ.get("CHALLENGE_STATE", "visible")
    s = requests.Session()
    # Content-Type is required even on GETs or CTFd ignores the token.
    s.headers.update({"Authorization": f"Token {token}", "Content-Type": "application/json"})

    r = s.get(f"{url}/api/v1/challenges?view=admin&per_page=100", timeout=15)
    r.raise_for_status()
    existing = {c["name"]: c["id"] for c in r.json().get("data", [])}

    for _, m in manifests:
        name = m["name"]
        desc = m.get("description") or f"Challenge: {name}"
        if m.get("author"):
            desc += f"\n\n_Author: {m['author']}_"
        if name in existing:
            body = {"description": desc, "category": m["category"]}
            resp = s.patch(f"{url}/api/v1/challenges/{existing[name]}", json=body, timeout=15)
            resp.raise_for_status()
            print(f"  updated  {name} (id={existing[name]})")
            continue
        initial = int(m.get("initial", 500))
        body = {
            "name": name,
            "category": m["category"],
            "description": desc,
            "type": "dynamic",
            "value": initial,
            "initial": initial,
            "minimum": int(m.get("minimum", 50)),
            "decay": int(m.get("decay", 15)),
            "state": state,
        }
        resp = s.post(f"{url}/api/v1/challenges", json=body, timeout=15)
        if not resp.ok:
            sys.exit(f"  failed to create {name}: HTTP {resp.status_code} {resp.text[:300]}")
        print(f"  created  {name} (id={resp.json()['data']['id']})")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("validate", "sync"):
        print(__doc__)
        return 2
    manifests = load()
    if not manifests:
        print("No manifests in challenges/manifests/ (schema.yaml is ignored).")
        return 1
    errors = validate(manifests)
    if errors:
        print("Manifest errors:")
        for e in errors:
            print(f"  ✘ {e}")
        return 1
    if sys.argv[1] == "validate":
        print(f"  {len(manifests)} manifest(s) OK")
        return 0
    sync(manifests)
    return 0


if __name__ == "__main__":
    sys.exit(main())
