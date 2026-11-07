"""Per-team zips for "static" (file download) challenges.

Every team gets its own flag, so a leaked file only works for the team that
downloaded it. Source directory for a challenge:
  - text files containing {{FLAG}}: copied with the placeholder replaced, or
  - build.sh (host / `make challenges` only): runs once per team with FLAG,
    TEAM_ID and OUT_DIR set, and writes the player's files to OUT_DIR.

Output: <out_dir>/<challenge_id>/<team_id>.zip, served by the static-files pod.
Shared by scripts/build-static.py (host) and the dashboard (controller-admin).
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

PLACEHOLDER = "{{FLAG}}"

# Limits for zips uploaded through the dashboard
MAX_UPLOAD_FILES = 500
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # uncompressed


class StaticBuildError(RuntimeError):
    pass


def zip_dir(directory: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(directory.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(directory))
    return buf.getvalue()


def has_placeholder(src: Path) -> bool:
    for p in src.rglob("*"):
        if p.is_file():
            try:
                if PLACEHOLDER in p.read_text(encoding="utf-8"):
                    return True
            except (UnicodeDecodeError, OSError):
                continue
    return False


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


def build_one(cid: str, src: Path, team_id: str, flag: str, allow_build_sh: bool) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "out"
        work.mkdir()
        if (src / "build.sh").exists():
            if not allow_build_sh:
                raise StaticBuildError(f"{cid}: build.sh is only supported with `make challenges`, "
                                       "not for dashboard uploads")
            env = {**os.environ, "FLAG": flag, "TEAM_ID": team_id, "OUT_DIR": str(work)}
            res = subprocess.run(["bash", "build.sh"], cwd=src, env=env,
                                 capture_output=True, text=True)
            if res.returncode != 0:
                raise StaticBuildError(f"{cid}: build.sh failed for team {team_id}:\n{res.stderr[-2000:]}")
            if not any(work.iterdir()):
                raise StaticBuildError(f"{cid}: build.sh wrote nothing to $OUT_DIR")
        elif not build_templated(src, flag, work):
            raise StaticBuildError(f"{cid}: no file contains {PLACEHOLDER} and there is no build.sh; "
                                   "players would get a file without their flag")
        return zip_dir(work)


def build_challenge(cid: str, src: Path, out_dir: Path, team_ids: list[str],
                    flag_for: Callable[[str, str], str], allow_build_sh: bool) -> int:
    """Write out_dir/<cid>/<team>.zip for every team. Returns the zip count.

    Builds into a temporary directory and swaps it in at the end, so players
    never download a half-built set.
    """
    if not src.is_dir():
        raise StaticBuildError(f"{cid}: no source files")
    final = out_dir / cid
    staging = out_dir / f".{cid}.new"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for team_id in team_ids:
        (staging / f"{team_id}.zip").write_bytes(
            build_one(cid, src, team_id, flag_for(team_id, cid), allow_build_sh))
    old = out_dir / f".{cid}.old"
    if old.exists():
        shutil.rmtree(old)
    if final.exists():
        final.rename(old)
    staging.rename(final)
    if old.exists():
        shutil.rmtree(old)
    return len(team_ids)


def safe_extract(zip_path: Path, dest: Path) -> int:
    """Extract an uploaded zip into dest, refusing anything unsafe.

    Rejects absolute paths, '..', symlinks, too many files or too many bytes,
    and requires at least one {{FLAG}} placeholder. Returns the file count.
    """
    if not zipfile.is_zipfile(zip_path):
        raise StaticBuildError("not a zip file")
    with zipfile.ZipFile(zip_path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if not infos:
            raise StaticBuildError("the zip is empty")
        if len(infos) > MAX_UPLOAD_FILES:
            raise StaticBuildError(f"too many files ({len(infos)} > {MAX_UPLOAD_FILES})")
        if sum(i.file_size for i in infos) > MAX_UPLOAD_BYTES:
            raise StaticBuildError(f"uncompressed size over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        for i in z.infolist():
            name = i.filename
            parts = Path(name).parts
            if name.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
                raise StaticBuildError(f"unsafe path in zip: {name}")
            if stat.S_ISLNK(i.external_attr >> 16):
                raise StaticBuildError(f"symlinks are not allowed: {name}")
            if Path(name).name == "build.sh":
                raise StaticBuildError("build.sh is only supported with `make challenges`; "
                                       "upload files containing {{FLAG}} instead")
        staging = dest.parent / f".{dest.name}.upload"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        z.extractall(staging)
    # A zip of a folder ("files/readme.txt") → use the folder's contents
    entries = list(staging.iterdir())
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
    if not has_placeholder(root):
        shutil.rmtree(staging)
        raise StaticBuildError(f"no file in the zip contains {PLACEHOLDER}; "
                               "players would get a file without their flag")
    if dest.exists():
        shutil.rmtree(dest)
    if root is staging:
        staging.rename(dest)
    else:
        root.rename(dest)
        shutil.rmtree(staging)
    return sum(1 for p in dest.rglob("*") if p.is_file())
