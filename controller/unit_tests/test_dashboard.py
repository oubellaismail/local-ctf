"""Tests for the dashboard backend: manifests, uploads, image tars, the deploy
pipeline and the admin API. No cluster, CTFd or registry needed."""

import io
import json
import os
import tarfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

import manifests as mf  # noqa: E402
import static_build  # noqa: E402
import registry  # noqa: E402
from flag import derive_flag  # noqa: E402
from jobs import Job, JobRunner  # noqa: E402


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("CTF_MASTER_SECRET", "test-master-secret")


def svc(**kw):
    m = {"id": "web-login", "name": "Login", "category": "Web", "type": "http", "port": 8080}
    m.update(kw)
    return mf.normalize(m)


# ─── Manifest validation ────────────────────────────────────────────────────

class TestValidate:
    def test_good_service(self):
        m = svc()
        assert m["image"] == "ctf-registry:5000/challenges/web-login:latest"
        assert m["memory_limit"] == "128Mi"
        assert mf.validate_one(m) == []

    @pytest.mark.parametrize("bad_id", ["Web", "-a", "a-", "a_b", "x" * 41, ""])
    def test_bad_ids(self, bad_id):
        assert any("id" in e for e in mf.validate_one(svc(id=bad_id)))

    def test_bad_port_and_memory(self):
        errs = mf.validate_one(svc(port=0, memory_limit="1Gi"))
        assert any("port" in e for e in errs)
        assert any("memory_limit" in e for e in errs)

    def test_port_bool_rejected(self):
        assert any("port" in e for e in mf.validate_one(svc(port=True)))

    def test_foreign_image_rejected(self):
        assert any("image" in e for e in mf.validate_one(svc(image="docker.io/evil:latest")))

    def test_unknown_field_strict(self):
        assert any("unknown" in e for e in mf.validate_one(svc(hostNetwork=True), strict_fields=True))
        assert mf.validate_one(svc(hostNetwork=True)) == []  # files from the repo: lenient

    def test_minimum_above_initial(self):
        assert any("minimum" in e for e in mf.validate_one(svc(initial=100, minimum=200)))

    def test_static_needs_files(self):
        m = {"id": "welcome", "name": "Welcome", "category": "Misc", "type": "static"}
        assert any("files" in e for e in mf.validate_one(m, lambda cid: False))
        assert mf.validate_one(m, lambda cid: True) == []

    def test_duplicates(self):
        errs = mf.validate_all([("a", svc()), ("b", svc())])
        assert any("duplicate id" in e for e in errs)
        assert any("duplicate name" in e for e in errs)

    def test_expand_bundle(self):
        bundle = {"id": "b", "name": "Bundle", "type": "http", "image": "x", "port": 1,
                  "flags": [{"id": "b-1", "name": "One", "env": "F1"}]}
        e = mf.expand_flag_entries([bundle, svc()])
        assert e[0]["_parent_id"] == "b" and e[0]["port"] == 1
        assert e[1]["id"] == "web-login"


# ─── ConfigMap store ────────────────────────────────────────────────────────

class FakeCore:
    """Just enough of CoreV1Api for ManifestStore."""

    def __init__(self, data=None):
        self.cm = SimpleNamespace(data=dict(data or {})) if data is not None else None

    def read_namespaced_config_map(self, name, ns):
        from kubernetes.client.rest import ApiException
        if self.cm is None:
            raise ApiException(status=404)
        return SimpleNamespace(data=dict(self.cm.data))

    def create_namespaced_config_map(self, ns, body):
        self.cm = SimpleNamespace(data=dict(body.data))

    def replace_namespaced_config_map(self, name, ns, cm):
        self.cm = SimpleNamespace(data=dict(cm.data))


class TestStore:
    def test_put_get_delete(self):
        core = FakeCore()
        store = mf.ManifestStore(core)
        store.put(svc())
        assert store.get("web-login")["name"] == "Login"
        assert list(core.cm.data) == ["web-login.yaml"]
        store.delete("web-login")
        assert store.all() == {}

    def test_put_replaces_other_key_with_same_id(self):
        core = FakeCore({"old-name.yaml": mf.dump(svc()), "schema.yaml": "# doc"})
        store = mf.ManifestStore(core)
        store.put(svc(name="Renamed"))
        assert "old-name.yaml" not in core.cm.data
        assert store.get("web-login")["name"] == "Renamed"
        assert "schema.yaml" in core.cm.data  # untouched, and ignored by all()
        assert list(store.all()) == ["web-login"]

    def test_clear(self):
        core = FakeCore({"a.yaml": mf.dump(svc())})
        mf.ManifestStore(core).clear()
        assert core.cm.data == {}


# ─── Uploaded zips (download challenges) ────────────────────────────────────

def make_zip(path, files, symlink=None):
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            z.writestr(info, "/etc/passwd")
    return path


class TestSafeExtract:
    def test_ok_and_folder_unwrapped(self, tmp_path):
        z = make_zip(tmp_path / "f.zip", {"chal/readme.txt": "flag: {{FLAG}}", "chal/data.bin": b"\x00\x01"})
        n = static_build.safe_extract(z, tmp_path / "src")
        assert n == 2
        assert (tmp_path / "src" / "readme.txt").exists()

    @pytest.mark.parametrize("name", ["../evil.txt", "/abs.txt", "a/../../b.txt"])
    def test_path_traversal(self, tmp_path, name):
        z = make_zip(tmp_path / "f.zip", {name: "{{FLAG}}"})
        with pytest.raises(static_build.StaticBuildError, match="unsafe"):
            static_build.safe_extract(z, tmp_path / "src")
        assert not (tmp_path / "evil.txt").exists()

    def test_symlink(self, tmp_path):
        z = make_zip(tmp_path / "f.zip", {"a.txt": "{{FLAG}}"}, symlink="link")
        with pytest.raises(static_build.StaticBuildError, match="symlink"):
            static_build.safe_extract(z, tmp_path / "src")

    def test_needs_placeholder(self, tmp_path):
        z = make_zip(tmp_path / "f.zip", {"a.txt": "no flag here"})
        with pytest.raises(static_build.StaticBuildError, match="FLAG"):
            static_build.safe_extract(z, tmp_path / "src")
        assert not (tmp_path / "src").exists()

    def test_build_sh_refused(self, tmp_path):
        z = make_zip(tmp_path / "f.zip", {"build.sh": "rm -rf /", "a.txt": "{{FLAG}}"})
        with pytest.raises(static_build.StaticBuildError, match="build.sh"):
            static_build.safe_extract(z, tmp_path / "src")

    def test_not_a_zip(self, tmp_path):
        p = tmp_path / "f.zip"
        p.write_text("hello")
        with pytest.raises(static_build.StaticBuildError, match="not a zip"):
            static_build.safe_extract(p, tmp_path / "src")


class TestBuildChallenge:
    def test_per_team_flags(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "readme.txt").write_text("your flag: {{FLAG}}")
        out = tmp_path / "out"
        out.mkdir()
        n = static_build.build_challenge("welcome", src, out, ["1", "2"], derive_flag, allow_build_sh=False)
        assert n == 2
        for team in ("1", "2"):
            with zipfile.ZipFile(out / "welcome" / f"{team}.zip") as z:
                assert derive_flag(team, "welcome") in z.read("readme.txt").decode()
        assert not list(out.glob(".*"))  # staging dirs cleaned up

    def test_build_sh_only_when_allowed(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "build.sh").write_text('echo "$FLAG" > "$OUT_DIR/f.txt"')
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(static_build.StaticBuildError):
            static_build.build_challenge("x", src, out, ["1"], derive_flag, allow_build_sh=False)
        assert static_build.build_challenge("x", src, out, ["1"], derive_flag, allow_build_sh=True) == 1


# ─── Image tars ─────────────────────────────────────────────────────────────

def make_tar(path, manifest):
    with tarfile.open(path, "w") as t:
        if manifest is not None:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return path


class TestImageTar:
    def test_ok(self, tmp_path):
        registry.check_image_tar(make_tar(tmp_path / "i.tar", [{"Config": "c.json", "Layers": []}]))

    def test_not_docker_save(self, tmp_path):
        with pytest.raises(registry.RegistryError, match="docker save"):
            registry.check_image_tar(make_tar(tmp_path / "i.tar", None))

    def test_multiple_images(self, tmp_path):
        with pytest.raises(registry.RegistryError, match="2 images"):
            registry.check_image_tar(make_tar(tmp_path / "i.tar", [{}, {}]))

    def test_not_tar(self, tmp_path):
        p = tmp_path / "i.tar"
        p.write_bytes(b"\x00" * 10)
        with pytest.raises(registry.RegistryError, match="not a tar"):
            registry.check_image_tar(p)


# ─── Jobs ───────────────────────────────────────────────────────────────────

class TestJobs:
    def _wait(self, job):
        for _ in range(100):
            if job.status in ("done", "failed"):
                return
            time.sleep(0.02)

    def test_done_and_failed(self):
        runner = JobRunner()

        def ok(job):
            with job.step("one"):
                job.log("hello")

        def bad(job):
            with job.step("boom"):
                raise RuntimeError("nope")

        j1, j2 = runner.submit("ok", ok), runner.submit("bad", bad)
        self._wait(j1)
        self._wait(j2)
        assert j1.status == "done" and j1.steps[0]["status"] == "done"
        assert j2.status == "failed" and j2.error == "nope"
        assert j2.steps[0]["status"] == "failed"
        assert any("hello" in line for line in j1.to_dict()["log"])


# ─── Deploy pipeline ────────────────────────────────────────────────────────

@pytest.fixture
def pipe(tmp_path, monkeypatch):
    import pipeline
    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    ctx = SimpleNamespace(apps_v1=MagicMock(), core_v1=MagicMock(), clients=(None, None, None),
                          store=mf.ManifestStore(FakeCore()))
    calls = []
    client = MagicMock()
    client.url = "http://ctfd"
    client.upsert_challenge.side_effect = lambda m, state_for_new: calls.append(("ctfd", m["id"])) or ("created", 1)
    client.team_ids.return_value = ["1", "2"]
    monkeypatch.setattr(pipeline, "ctfd", lambda: client)
    monkeypatch.setattr(pipeline, "admin_token", lambda: "tok")
    monkeypatch.setattr(pipeline, "restart_controller_api", lambda c, j: calls.append(("restart",)))
    monkeypatch.setattr(pipeline.registry, "push_tar",
                        lambda p, cid: calls.append(("push", cid)) or f"reg/{cid}")
    monkeypatch.setattr(pipeline.bulk_spawn, "run", lambda url, tok, manifests, clients:
                        calls.append(("spawn", [m["id"] for m in manifests])) or
                        {"created": 2, "skipped": 0, "errors": 0})
    monkeypatch.setattr(pipeline.import_flags, "run", lambda url, tok, manifests:
                        calls.append(("flags", [m["id"] for m in manifests])) or
                        {"created": len(manifests), "skipped": 0, "errors": 0})
    return pipeline, ctx, calls, tmp_path


class TestPipeline:
    def test_service_with_pending_image(self, pipe):
        pipeline, ctx, calls, data = pipe
        ctx.store.put(svc())
        tar = pipeline.pending_image("web-login")
        tar.parent.mkdir(parents=True)
        tar.write_bytes(b"x")
        job = Job("t")
        pipeline.deploy(ctx, job, ids=["web-login"])
        assert [c[0] for c in calls] == ["push", "ctfd", "restart", "spawn", "flags"]
        assert not tar.exists()
        assert [s["status"] for s in job.steps] == ["done"] * 6

    def test_static_from_upload(self, pipe):
        pipeline, ctx, calls, data = pipe
        ctx.store.put({"id": "welcome", "name": "Welcome", "category": "Misc", "type": "static"})
        src = pipeline.static_src("welcome")
        src.mkdir(parents=True)
        (src / "r.txt").write_text("{{FLAG}}")
        pipeline.deploy(ctx, Job("t"))
        assert ("restart",) not in calls  # no service challenge → no reload
        assert pipeline.static_zip_count("welcome") == 2

    def test_invalid_manifest_stops_before_ctfd(self, pipe):
        pipeline, ctx, calls, data = pipe
        ctx.store.put({"id": "welcome", "name": "Welcome", "category": "Misc", "type": "static"})
        job = Job("t")
        with pytest.raises(RuntimeError, match="no files"):
            pipeline.deploy(ctx, job)
        assert calls == []
        assert job.steps[0]["status"] == "failed"

    def test_deploy_all_skips_static_without_files(self, pipe):
        pipeline, ctx, calls, data = pipe
        ctx.store.put(svc())
        ctx.store.put({"id": "welcome", "name": "Welcome", "category": "Misc", "type": "static"})
        job = Job("t")
        pipeline.deploy(ctx, job)  # ids=None: deploy all
        assert ("spawn", ["web-login"]) in calls
        assert ("ctfd", "welcome") not in calls
        assert "skipped welcome" in job.steps[0]["detail"]

    def test_single_deploy_without_files_still_fails(self, pipe):
        pipeline, ctx, calls, data = pipe
        ctx.store.put({"id": "welcome", "name": "Welcome", "category": "Misc", "type": "static"})
        with pytest.raises(RuntimeError, match="no files"):
            pipeline.deploy(ctx, Job("t"), ids=["welcome"])

    def test_unknown_id(self, pipe):
        pipeline, ctx, calls, data = pipe
        with pytest.raises(RuntimeError, match="unknown"):
            pipeline.deploy(ctx, Job("t"), ids=["nope"])


# ─── Admin API ──────────────────────────────────────────────────────────────

@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "k")
    import importlib
    import admin_api
    admin_api = importlib.reload(admin_api)
    import pipeline
    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    ctx = SimpleNamespace(apps_v1=MagicMock(), core_v1=MagicMock(), store=mf.ManifestStore(FakeCore()))
    ctx.apps_v1.list_namespaced_deployment.return_value.items = []
    monkeypatch.setattr(admin_api, "ctx", lambda: ctx)
    monkeypatch.setattr(pipeline, "ctfd", lambda: (_ for _ in ()).throw(RuntimeError("no ctfd")))
    from fastapi.testclient import TestClient
    return TestClient(admin_api.app), ctx, tmp_path


H = {"X-Admin-Key": "k"}


class TestAdminAPI:
    def test_auth(self, admin):
        client, _, _ = admin
        assert client.get("/challenges").status_code == 403
        assert client.get("/challenges", headers={"X-Admin-Key": "wrong"}).status_code == 403
        assert client.get("/health").status_code == 200

    def test_save_and_list(self, admin):
        client, ctx, _ = admin
        r = client.put("/challenges/web-login", headers=H,
                       json={"name": "Login", "category": "Web", "type": "http", "port": 8080, "description": ""})
        assert r.status_code == 200, r.text
        assert r.json()["manifest"]["image"].endswith("challenges/web-login:latest")
        rows = client.get("/challenges", headers=H).json()
        assert rows["challenges"][0]["manifest"]["id"] == "web-login"
        assert "no ctfd" in rows["ctfd_error"]

    def test_save_rejects_bad_input(self, admin):
        client, _, _ = admin
        assert client.put("/challenges/Bad_ID", headers=H, json={}).status_code == 400
        r = client.put("/challenges/x", headers=H, json={"name": "X", "category": "W", "type": "http",
                                                         "port": 1, "hostNetwork": True})
        assert r.status_code == 422 and "unknown" in json.dumps(r.json())

    def test_type_cannot_change(self, admin):
        client, ctx, _ = admin
        ctx.store.put(svc())
        r = client.put("/challenges/web-login", headers=H,
                       json={"name": "Login", "category": "Web", "type": "static"})
        assert r.status_code == 422

    def test_upload_image_tar(self, admin, tmp_path):
        client, ctx, data = admin
        ctx.store.put(svc())
        tar = make_tar(tmp_path / "i.tar", [{"Config": "c", "Layers": []}])
        r = client.post("/challenges/web-login/upload", headers=H, content=tar.read_bytes())
        assert r.status_code == 200, r.text
        assert (data / "uploads" / "web-login" / "image.tar").exists()
        bad = client.post("/challenges/web-login/upload", headers=H, content=b"not a tar")
        assert bad.status_code == 422

    def test_upload_zip(self, admin, tmp_path):
        client, ctx, data = admin
        ctx.store.put({"id": "welcome", "name": "Welcome", "category": "Misc", "type": "static"})
        z = make_zip(tmp_path / "f.zip", {"readme.txt": "{{FLAG}}"})
        r = client.post("/challenges/welcome/upload", headers=H, content=z.read_bytes())
        assert r.status_code == 200, r.text
        assert (data / "static-src" / "welcome" / "readme.txt").exists()

    def test_upload_needs_saved_challenge(self, admin):
        client, _, _ = admin
        assert client.post("/challenges/ghost/upload", headers=H, content=b"x").status_code == 404


# ─── v1.2: flag modes and scoring ───────────────────────────────────────────

class TestFlagModes:
    def test_static_needs_flag(self):
        assert any("needs its 'flag'" in e for e in mf.validate_one(svc(flag_mode="static")))
        assert mf.validate_one(svc(flag_mode="static", flag="CTF{same}")) == []

    def test_bad_static_flags(self):
        assert any("one line" in e for e in mf.validate_one(svc(flag_mode="static", flag="a\nb")))
        assert any("one line" in e for e in mf.validate_one(svc(flag_mode="static", flag=" x")))
        assert any("max" in e for e in mf.validate_one(svc(flag_mode="static", flag="x" * 201)))
        assert any("only used" in e for e in mf.validate_one(svc(flag="CTF{x}")))
        assert any("flag_mode" in e for e in mf.validate_one(svc(flag_mode="weird")))

    def test_bundle_cannot_be_static(self):
        m = svc(flag_mode="static", flag="X", flags=[{"id": "a", "name": "A", "env": "F"}])
        assert any("bundle" in e for e in mf.validate_one(m))

    def test_flag_for(self):
        assert mf.flag_for(svc(flag_mode="static", flag="CTF{same}"), "7", derive_flag) == "CTF{same}"
        assert mf.flag_for(svc(), "7", derive_flag) == derive_flag("7", "web-login")

    def test_scoring(self):
        assert any("value" in e for e in mf.validate_one(svc(scoring="fixed")))
        assert mf.validate_one(svc(scoring="fixed", value=100)) == []
        assert any("scoring" in e for e in mf.validate_one(svc(scoring="nope")))

    def test_instance_secret_uses_static_flag(self):
        import bulk_spawn
        data, _ = bulk_spawn._flag_env_vars("3", svc(flag_mode="static", flag="CTF{same}"))
        assert data == {"FLAG": "CTF{same}"}
        data, _ = bulk_spawn._flag_env_vars("3", svc())
        assert data == {"FLAG": derive_flag("3", "web-login")}


class FakeHttpx:
    """Records flag API calls for import_flags.sync_flag."""

    def __init__(self, flags):
        self.flags = flags
        self.calls = []

    def _resp(self, payload=None):
        r = MagicMock()
        r.json.return_value = payload or {}
        r.raise_for_status.return_value = None
        return r

    def get(self, url, **kw):
        return self._resp({"data": self.flags})

    def delete(self, url, **kw):
        self.calls.append(("delete", url.rsplit("/", 1)[1]))
        return self._resp()

    def post(self, url, json=None, **kw):
        self.calls.append(("post", json["type"], json["content"]))
        return self._resp()


class TestSyncFlag:
    def run(self, monkeypatch, flags, manifest):
        import import_flags
        fake = FakeHttpx(flags)
        monkeypatch.setattr(import_flags, "httpx", fake)
        result = import_flags.sync_flag("http://ctfd", "tok", manifest, {manifest["name"]: 5})
        return result, fake.calls

    def test_new_team_flag(self, monkeypatch):
        result, calls = self.run(monkeypatch, [], svc())
        assert result == "created" and calls == [("post", "team", "team-derived")]

    def test_existing_team_flag(self, monkeypatch):
        result, calls = self.run(monkeypatch, [{"id": 1, "type": "team", "content": "team-derived"}], svc())
        assert result == "skipped" and calls == []

    def test_switch_to_static(self, monkeypatch):
        result, calls = self.run(monkeypatch, [{"id": 1, "type": "team", "content": "team-derived"}],
                                 svc(flag_mode="static", flag="CTF{same}"))
        assert result == "updated"
        assert calls == [("delete", "1"), ("post", "static", "CTF{same}")]

    def test_static_flag_changed(self, monkeypatch):
        result, calls = self.run(monkeypatch, [{"id": 2, "type": "static", "content": "CTF{old}"}],
                                 svc(flag_mode="static", flag="CTF{new}"))
        assert calls == [("delete", "2"), ("post", "static", "CTF{new}")]

    def test_back_to_per_team(self, monkeypatch):
        result, calls = self.run(monkeypatch, [{"id": 2, "type": "static", "content": "CTF{old}"}], svc())
        assert calls == [("delete", "2"), ("post", "team", "team-derived")]


class TestCTFdPoints:
    def test_points(self):
        from ctfd_api import CTFd
        assert CTFd._points(svc(scoring="fixed", value=150)) == ("standard", {"value": 150})
        assert CTFd._points(svc(initial=400, minimum=40, decay=10)) == (
            "dynamic", {"initial": 400, "minimum": 40, "decay": 10})

    def test_create_fixed_and_refuse_type_switch(self):
        from ctfd_api import CTFd, CTFdError
        c = CTFd("http://ctfd", "tok")
        sent = []
        c._req = lambda method, path, **kw: sent.append((method, path, kw.get("json"))) or {"data": {"id": 9}}
        c.challenges = lambda: {}
        assert c.upsert_challenge(svc(scoring="fixed", value=150)) == ("created", 9)
        body = sent[0][2]
        assert body["type"] == "standard" and body["value"] == 150 and "initial" not in body
        c.challenges = lambda: {"Login": {"id": 9, "type": "standard"}}
        assert c.upsert_challenge(svc(scoring="fixed", value=200))[0] == "updated"
        assert sent[-1][2]["value"] == 200
        with pytest.raises(CTFdError, match="Delete and re-create"):
            c.upsert_challenge(svc())  # dynamic, but CTFd has it as standard


class TestPipelineV12:
    def test_static_flag_zips_and_map(self, pipe):
        pipeline, ctx, calls, data = pipe
        ctx.store.put({"id": "welcome", "name": "Welcome", "category": "Misc", "type": "static",
                       "flag_mode": "static", "flag": "CTF{same}"})
        src = pipeline.static_src("welcome")
        src.mkdir(parents=True)
        (src / "r.txt").write_text("flag: {{FLAG}}")
        pipeline.deploy(ctx, Job("t"))
        for team in ("1", "2"):
            with zipfile.ZipFile(pipeline.static_out() / "welcome" / f"{team}.zip") as z:
                assert z.read("r.txt").decode() == "flag: CTF{same}"
        pipeline.ctfd().update_challenge_map.assert_called_with(add={"welcome": 1})
