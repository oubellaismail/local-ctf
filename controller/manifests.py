"""Challenge manifests: validation and storage.

One manifest = one challenge (YAML dict). They live in the ConfigMap
ctfd/challenge-manifests, one key per challenge ("<id>.yaml"). Both the
dashboard (controller-admin) and `make challenges` write there, and
everything that deploys challenges reads from there.

Used by the controller (in the cluster) and by scripts/ on the host, so this
module only needs PyYAML at import time.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

import yaml

REGISTRY_PREFIX = "ctf-registry:5000/"
ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
TYPES = ("http", "tcp", "static")
SERVICE_TYPES = ("http", "tcp")
MEMORY_RE = re.compile(r"^([1-9][0-9]*)Mi$")
MAX_MEMORY_MI = 512

CONFIGMAP = "challenge-manifests"
NAMESPACE = "ctfd"

# Fields the dashboard form may set. Anything else in an uploaded/edited
# manifest is rejected, so a typo can't silently do nothing.
EDITABLE_FIELDS = {
    "id", "name", "category", "type", "image", "port", "container_port",
    "extra_ports", "memory_limit", "run_as_root", "allow_privilege_escalation",
    "description", "author", "initial", "minimum", "decay",
    "flag_mode", "flag", "scoring", "value",
}

# flag_mode: "per-team" (default) = each team's flag is derived from the
#   master secret, so a shared flag is rejected and logged as cheating.
#   "static" = one flag for everyone (the manifest's `flag`); sharing can't
#   be detected.
FLAG_MODES = ("per-team", "static")
# scoring: "dynamic" (default) = initial/minimum/decay; "fixed" = `value`
SCORINGS = ("dynamic", "fixed")
MAX_FLAG_LEN = 200


def flag_mode(m: dict) -> str:
    return m.get("flag_mode") or "per-team"


def scoring(m: dict) -> str:
    return m.get("scoring") or "dynamic"


def flag_for(m: dict, team_id: str, derive) -> str:
    """The flag team `team_id` must submit for manifest m."""
    if flag_mode(m) == "static":
        return m["flag"]
    return derive(team_id, m["id"])


def default_image(challenge_id: str) -> str:
    return f"{REGISTRY_PREFIX}challenges/{challenge_id}:latest"


def normalize(m: dict) -> dict:
    """Fill defaults the rest of the code relies on."""
    m = dict(m)
    if m.get("type") in SERVICE_TYPES:
        m.setdefault("memory_limit", "128Mi")
        if not m.get("image") and m.get("id"):
            m["image"] = default_image(m["id"])
    return m


def validate_one(m: dict, has_static_source: Optional[Callable[[str], bool]] = None,
                 strict_fields: bool = False) -> list[str]:
    """Problems with one manifest, as human-readable strings (empty = OK)."""
    errors: list[str] = []
    cid = str(m.get("id", ""))
    if not ID_RE.match(cid):
        errors.append(f"id '{cid}' must be lowercase letters, digits and hyphens, max 40 "
                      "characters (it becomes part of Kubernetes names)")
    for field in ("name", "category", "type"):
        if not m.get(field):
            errors.append(f"missing '{field}'")
    if strict_fields:
        unknown = sorted(set(m) - EDITABLE_FIELDS)
        if unknown:
            errors.append(f"unknown field(s): {', '.join(unknown)}")
    ctype = m.get("type")
    if ctype and ctype not in TYPES:
        errors.append(f"type must be http, tcp or static (got '{ctype}')")
    if ctype in SERVICE_TYPES:
        port = m.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            errors.append("service challenges need a 'port' between 1 and 65535")
        image = str(m.get("image", ""))
        if not image.startswith(REGISTRY_PREFIX):
            errors.append(f"image must start with {REGISTRY_PREFIX} (got '{image}')")
        mem = str(m.get("memory_limit", "128Mi"))
        mm = MEMORY_RE.match(mem)
        if not mm or int(mm.group(1)) > MAX_MEMORY_MI:
            errors.append(f"memory_limit must be like 128Mi, at most {MAX_MEMORY_MI}Mi (got '{mem}')")
    if ctype == "static" and has_static_source is not None and not has_static_source(cid):
        errors.append("static challenge has no files yet (upload a .zip)")
    for field in ("initial", "minimum", "decay", "value"):
        if field in m and (not isinstance(m[field], int) or isinstance(m[field], bool) or m[field] < 0):
            errors.append(f"'{field}' must be a whole number ≥ 0")
    if isinstance(m.get("initial"), int) and isinstance(m.get("minimum"), int) and m["minimum"] > m["initial"]:
        errors.append("'minimum' can't be higher than 'initial'")
    mode = flag_mode(m)
    if mode not in FLAG_MODES:
        errors.append(f"flag_mode must be per-team or static (got '{mode}')")
    if mode == "static":
        fl = m.get("flag")
        if not isinstance(fl, str) or not fl.strip():
            errors.append("a static flag challenge needs its 'flag'")
        elif len(fl) > MAX_FLAG_LEN or "\n" in fl or fl != fl.strip():
            errors.append(f"'flag' must be one line, no leading/trailing spaces, max {MAX_FLAG_LEN} characters")
        if "flags" in m:
            errors.append("multi-flag (bundle) challenges can't use a static flag")
    elif m.get("flag"):
        errors.append("'flag' is only used with flag_mode: static")
    sc = scoring(m)
    if sc not in SCORINGS:
        errors.append(f"scoring must be dynamic or fixed (got '{sc}')")
    if sc == "fixed":
        v = m.get("value")
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            errors.append("fixed scoring needs a 'value' (points, whole number ≥ 0)")
    if "flags" in m:
        for i, fl in enumerate(m["flags"] or []):
            for field in ("id", "name", "env"):
                if not fl.get(field):
                    errors.append(f"flags[{i}] missing '{field}'")
    return errors


def validate_all(manifests: Iterable[tuple[str, dict]],
                 has_static_source: Optional[Callable[[str], bool]] = None) -> list[str]:
    """Validate several manifests, labelled by `where` (file name or key).

    Also checks uniqueness of ids and names (CTFd matches challenges by name).
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for where, m in manifests:
        errors += [f"{where}: {e}" for e in validate_one(m, has_static_source)]
        cid, name = m.get("id"), m.get("name")
        if cid in seen_ids:
            errors.append(f"{where}: duplicate id '{cid}'")
        if name in seen_names:
            errors.append(f"{where}: duplicate name '{name}' (CTFd matches challenges by name)")
        seen_ids.add(cid)
        seen_names.add(name)
    return errors


def expand_flag_entries(manifests: Iterable[dict]) -> list[dict]:
    """One entry per CTFd flag. Bundle manifests (with 'flags') are expanded;
    see manifest_loader.load_flag_entries, which this mirrors."""
    entries = []
    for manifest in manifests:
        if "flags" in manifest:
            for flag in manifest["flags"]:
                entries.append({
                    "image": manifest.get("image"),
                    "port": manifest.get("port"),
                    "type": manifest.get("type"),
                    "memory_limit": manifest.get("memory_limit", "128Mi"),
                    "extra_ports": manifest.get("extra_ports", []),
                    **flag,
                    "_parent_id": manifest["id"],
                    "_bundle_name": manifest.get("name", manifest["id"]),
                })
        else:
            entries.append(manifest)
    return entries


def dump(m: dict) -> str:
    return yaml.safe_dump(m, sort_keys=False, allow_unicode=True)


def key_for(challenge_id: str) -> str:
    return f"{challenge_id}.yaml"


class ManifestStore:
    """Read/write manifests in the challenge-manifests ConfigMap."""

    def __init__(self, core_v1, namespace: str = NAMESPACE, name: str = CONFIGMAP):
        self.core_v1 = core_v1
        self.namespace = namespace
        self.name = name

    def _read(self):
        from kubernetes.client.rest import ApiException
        try:
            return self.core_v1.read_namespaced_config_map(self.name, self.namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def all(self) -> dict[str, dict]:
        """{challenge_id: manifest}, sorted by id."""
        cm = self._read()
        out: dict[str, dict] = {}
        for key, text in sorted(((cm.data or {}) if cm else {}).items()):
            if key == "schema.yaml" or not key.endswith(".yaml"):
                continue
            m = yaml.safe_load(text) or {}
            if m.get("id"):
                out[m["id"]] = normalize(m)
        return out

    def get(self, challenge_id: str) -> Optional[dict]:
        return self.all().get(challenge_id)

    def _write(self, mutate: Callable[[dict], None]) -> None:
        from kubernetes import client
        from kubernetes.client.rest import ApiException
        for _ in range(5):  # optimistic concurrency: retry on conflict
            cm = self._read()
            if cm is None:
                data: dict = {}
                mutate(data)
                body = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=self.name), data=data)
                try:
                    self.core_v1.create_namespaced_config_map(self.namespace, body)
                    return
                except ApiException as e:
                    if e.status == 409:
                        continue
                    raise
            data = dict(cm.data or {})
            mutate(data)
            cm.data = data
            try:
                self.core_v1.replace_namespaced_config_map(self.name, self.namespace, cm)
                return
            except ApiException as e:
                if e.status == 409:
                    continue
                raise
        raise RuntimeError("challenge-manifests kept changing; try again")

    def put(self, m: dict) -> None:
        cid = m["id"]

        def mutate(data: dict) -> None:
            # Remove any other key holding the same id (e.g. "web.yaml"
            # written by make challenges for id "web-login").
            for k in list(data):
                if k != key_for(cid):
                    try:
                        if (yaml.safe_load(data[k]) or {}).get("id") == cid:
                            del data[k]
                    except yaml.YAMLError:
                        pass
            data[key_for(cid)] = dump(m)
        self._write(mutate)

    def delete(self, challenge_id: str) -> None:
        def mutate(data: dict) -> None:
            for k in list(data):
                try:
                    if (yaml.safe_load(data[k]) or {}).get("id") == challenge_id:
                        del data[k]
                except yaml.YAMLError:
                    pass
        self._write(mutate)

    def clear(self) -> None:
        self._write(lambda data: data.clear())
