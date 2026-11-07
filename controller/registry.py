"""Push an uploaded `docker save` tarball to the platform registry with crane.

No Docker daemon and no image build in the cluster: the tar already contains
the finished image layers, crane just uploads them.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path


class RegistryError(RuntimeError):
    pass


def check_image_tar(path: Path) -> None:
    """Make sure this is a single-image `docker save` archive."""
    if not tarfile.is_tarfile(path):
        raise RegistryError("not a tar file. Create it with: docker save -o chal.tar <image>")
    with tarfile.open(path) as t:
        try:
            member = t.getmember("manifest.json")
        except KeyError:
            raise RegistryError("no manifest.json inside: this isn't a `docker save` archive. "
                                "Create it with: docker save -o chal.tar <image>")
        f = t.extractfile(member)
        try:
            manifest = json.load(f) if f else None
        except ValueError:
            manifest = None
    if not isinstance(manifest, list) or not manifest:
        raise RegistryError("manifest.json in the tar is unreadable")
    if len(manifest) != 1:
        raise RegistryError(f"the tar holds {len(manifest)} images; save exactly one "
                            "(docker save -o chal.tar <image>)")


def push_ref(challenge_id: str) -> str:
    host = os.environ.get("REGISTRY_PUSH", "")
    if not host:
        raise RegistryError("REGISTRY_PUSH is not set (controller-admin config)")
    return f"{host}/challenges/{challenge_id}:latest"


def push_tar(path: Path, challenge_id: str, timeout: int = 900) -> str:
    """Push the tar as challenges/<id>:latest. Returns the pushed reference."""
    check_image_tar(path)
    ref = push_ref(challenge_id)
    host = ref.split("/", 1)[0]
    user = os.environ.get("REGISTRY_USER", "")
    password = os.environ.get("REGISTRY_PASSWORD", "")
    with tempfile.TemporaryDirectory() as cfg:
        if user:
            auth = base64.b64encode(f"{user}:{password}".encode()).decode()
            Path(cfg, "config.json").write_text(json.dumps({"auths": {host: {"auth": auth}}}))
        env = {**os.environ, "DOCKER_CONFIG": cfg, "HOME": cfg}
        try:
            res = subprocess.run(["crane", "push", "--insecure", str(path), ref],
                                 env=env, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise RegistryError("crane is not installed in the controller image (make build-controller)")
        except subprocess.TimeoutExpired:
            raise RegistryError(f"push took longer than {timeout // 60} minutes")
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        raise RegistryError("push failed: " + (msg[-1] if msg else f"exit {res.returncode}"))
    return ref
