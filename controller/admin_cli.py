#!/usr/bin/env python3
"""admin_cli.py: run the deploy pipeline from the command line.

Used by `make challenges` (kubectl exec into controller-admin):

    python3 admin_cli.py deploy              all challenges in the ConfigMap
    python3 admin_cli.py deploy --ids a,b    only these
    python3 admin_cli.py clear               empty the ConfigMap and files (new round)

Same code as the dashboard's Deploy button, so both behave the same.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

import pipeline
from jobs import run_inline
from k8s_client import init_k8s_client


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deploy")
    d.add_argument("--ids", default="")
    d.add_argument("--state", default=os.environ.get("CHALLENGE_STATE", "visible"),
                   choices=("visible", "hidden"), help="state of newly created CTFd challenges")
    sub.add_parser("clear")
    args = p.parse_args()

    ctx = pipeline.Context(*init_k8s_client())

    if args.cmd == "clear":
        ctx.store.clear()
        for name in ("static", "static-src", "uploads"):
            d = pipeline.DATA_DIR / name
            if d.is_dir():
                for child in d.iterdir():
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
        print("challenge manifests and uploaded files cleared")
        return 0

    ids = [i for i in args.ids.split(",") if i] or None
    job = run_inline("deploy", lambda j: pipeline.deploy(ctx, j, ids=ids, state_for_new=args.state),
                     echo=lambda msg: print(f"    {msg}", flush=True))
    if job.status != "done":
        print(f"ERROR {job.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
