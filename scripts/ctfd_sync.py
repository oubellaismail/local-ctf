#!/usr/bin/env python3
"""
ctfd_sync.py: check the challenge manifests in challenges/manifests and turn
them into a ConfigMap patch. Used by scripts/deploy-challenges.sh.

  validate   check every manifest (no network)
  patch      print a JSON merge patch adding/updating each manifest in the
             challenge-manifests ConfigMap (keys "<id>.yaml"). Challenges added
             from the dashboard are left alone.

The checks are the same ones the dashboard and the deploy pipeline use
(controller/manifests.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "controller"))
import manifests as mf  # noqa: E402

MANIFESTS = ROOT / "challenges" / "manifests"
STATIC = ROOT / "challenges" / "static"


def load() -> list[tuple[str, dict]]:
    out = []
    for f in sorted(MANIFESTS.glob("*.yaml")):
        if f.name == "schema.yaml":
            continue
        out.append((f.name, mf.normalize(yaml.safe_load(f.read_text()) or {})))
    return out


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("validate", "patch"):
        print(__doc__)
        return 2
    manifests = load()
    if sys.argv[1] == "validate":
        if not manifests:
            print("  no manifests in challenges/manifests/ (fine if you use the dashboard)")
            return 0
        errors = mf.validate_all(manifests, lambda cid: (STATIC / cid).is_dir())
        if errors:
            print("Manifest errors:")
            for e in errors:
                print(f"  ✘ {e}")
            return 1
        print(f"  {len(manifests)} manifest(s) OK")
        return 0
    data = {mf.key_for(m["id"]): mf.dump(m) for _, m in manifests}
    print(json.dumps({"data": data}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
