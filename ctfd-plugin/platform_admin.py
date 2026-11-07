"""Admin → Platform: add, upload, deploy, show/hide and delete challenges.

The page runs inside CTFd's admin panel. Its API calls go to
/admin/platform/api/..., which this module forwards to controller-admin
(the cluster-side service that pushes images and creates team instances)
with the shared admin key. Only CTFd admins get through (@admins_only).

CSRF: CTFd's built-in check only accepts the CSRF-Token header for JSON
requests, so every API call from the page is JSON, except uploads (raw file
body), which skip CTFd's check and verify the same header here instead.
"""

import hmac
import os
import re

import requests
from flask import Blueprint, Response, jsonify, render_template_string, request, session

ADMIN_URL = os.environ.get("CONTROLLER_ADMIN_URL", "http://controller-admin.ctfd.svc.cluster.local:5001")
ADMIN_KEY = os.environ.get("CONTROLLER_ADMIN_KEY", "")

# Only these controller-admin paths can be reached through the JSON proxy
ALLOWED = re.compile(
    r"^(challenges(/[a-z0-9-]{1,40}(/(deploy|visibility))?)?|deploy-all|jobs(/[a-f0-9]{1,32})?)$"
)
CHALLENGE_ID = re.compile(r"^[a-z0-9-]{1,40}$")


def _forward(method, path, data, content_type, timeout):
    url = f"{ADMIN_URL}/{path}"
    if request.query_string:
        url += "?" + request.query_string.decode()
    headers = {"X-Admin-Key": ADMIN_KEY, "Content-Type": content_type}
    try:
        r = requests.request(method, url, data=data, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return jsonify({"detail": f"controller-admin unreachable: {e.__class__.__name__}"}), 502
    return Response(r.content, status=r.status_code,
                    content_type=r.headers.get("Content-Type", "application/json"))


def register_platform_admin(app):
    from CTFd.plugins import register_admin_plugin_menu_bar
    from CTFd.utils.decorators import admins_only

    bp = Blueprint("platform_admin", __name__)

    @bp.route("/admin/platform")
    @admins_only
    def platform_page():
        return render_template_string(PAGE, nonce=session.get("nonce", ""),
                                      configured=bool(ADMIN_KEY))

    not_configured = ({"detail": "CONTROLLER_ADMIN_KEY is not set in CTFd (make secrets && make deploy)"}, 503)

    @bp.route("/admin/platform/api/<path:sub>", methods=["GET", "POST", "PUT", "DELETE"])
    @admins_only
    def platform_api(sub):
        # JSON only (CTFd has already checked the CSRF-Token header)
        if not ALLOWED.match(sub):
            return jsonify({"detail": "not allowed"}), 404
        if not ADMIN_KEY:
            return jsonify(not_configured[0]), not_configured[1]
        data = request.get_data() if request.method != "GET" else None
        return _forward(request.method, sub, data or None, "application/json", (5, 60))

    def platform_upload(cid):
        # Raw file body: CTFd's CSRF check would want a form field, so it's
        # skipped for this view (_bypass_csrf below) and done here.
        if not hmac.compare_digest(request.headers.get("CSRF-Token", ""), session.get("nonce", "") or "-"):
            return jsonify({"detail": "CSRF check failed: reload the page"}), 403
        if not CHALLENGE_ID.match(cid):
            return jsonify({"detail": "invalid challenge id"}), 400
        if not ADMIN_KEY:
            return jsonify(not_configured[0]), not_configured[1]
        # Stream the body (image tar / zip) through without loading it
        return _forward("POST", f"challenges/{cid}/upload", request.stream,
                        "application/octet-stream", (5, 1800))

    platform_upload = admins_only(platform_upload)
    platform_upload._bypass_csrf = True  # CTFd's csrf() skips views with this attribute
    bp.add_url_rule("/admin/platform/api/challenges/<cid>/upload", "platform_upload",
                    platform_upload, methods=["POST"])

    app.register_blueprint(bp)
    register_admin_plugin_menu_bar(title="Platform", route="/admin/platform")


PAGE = r"""
{% extends "admin/base.html" %}
{% block content %}
<div class="jumbotron">
  <div class="container">
    <h1>Platform</h1>
    <p class="text-muted mb-0">Add challenges, upload their image or files, and deploy them to every team.</p>
  </div>
</div>
<div class="container" id="pf" data-nonce="{{ nonce }}" data-configured="{{ 1 if configured else 0 }}">
  <div id="pf-alert"></div>

  <div style="gap:.5rem" class="d-flex gap-2 mb-3">
    <button class="btn btn-primary" id="pf-new">+ New challenge</button>
    <button class="btn btn-outline-secondary" id="pf-deploy-all">Deploy all</button>
    <button class="btn btn-outline-secondary ms-auto ml-auto" id="pf-refresh">Refresh</button>
  </div>

  <!-- Editor -->
  <div class="card mb-4 d-none" id="pf-editor">
    <div class="card-body">
      <h4 class="card-title" id="pf-editor-title">New challenge</h4>
      <form id="pf-form" autocomplete="off">
        <div class="row g-3">
          <div class="col-md-6">
            <label class="form-label">Name</label>
            <input class="form-control" name="name" required maxlength="80">
          </div>
          <div class="col-md-6">
            <label class="form-label">ID <small class="text-muted">(lowercase, digits, hyphens; part of every flag, can't change later)</small></label>
            <input class="form-control" name="id" required pattern="[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?">
          </div>
          <div class="col-md-4">
            <label class="form-label">Category</label>
            <input class="form-control" name="category" required list="pf-cats">
            <datalist id="pf-cats"><option>Web</option><option>Pwn</option><option>Crypto</option><option>Forensics</option><option>Reversing</option><option>Misc</option><option>OSINT</option></datalist>
          </div>
          <div class="col-md-4">
            <label class="form-label">Type</label>
            <select class="form-control form-select" name="type">
              <option value="http">Web service (players get a URL)</option>
              <option value="tcp">TCP service (players get nc host port)</option>
              <option value="static">Download (per-team files)</option>
            </select>
          </div>
          <div class="col-md-4 pf-service">
            <label class="form-label">Port the container listens on</label>
            <input class="form-control" name="port" type="number" min="1" max="65535" value="8080">
          </div>
          <div class="col-md-4 pf-service">
            <label class="form-label">Memory per instance</label>
            <select class="form-control form-select" name="memory_limit">
              <option>128Mi</option><option>256Mi</option><option>512Mi</option>
            </select>
          </div>
          <div class="col-md-8 pf-service">
            <label class="form-label">Image</label>
            <input class="form-control" name="image" readonly placeholder="set automatically: ctf-registry:5000/challenges/&lt;id&gt;:latest">
          </div>
          <div class="col-md-4">
            <label class="form-label">Points (start)</label>
            <input class="form-control" name="initial" type="number" min="0" value="500">
          </div>
          <div class="col-md-4">
            <label class="form-label">Points (minimum)</label>
            <input class="form-control" name="minimum" type="number" min="0" value="50">
          </div>
          <div class="col-md-4">
            <label class="form-label">Solves to reach minimum</label>
            <input class="form-control" name="decay" type="number" min="0" value="15">
          </div>
          <div class="col-12">
            <label class="form-label">Description <small class="text-muted">(Markdown)</small></label>
            <textarea class="form-control" name="description" rows="4"></textarea>
          </div>
          <div class="col-md-6">
            <label class="form-label">Author</label>
            <input class="form-control" name="author">
          </div>
        </div>
        <div class="mt-3 d-flex gap-2">
          <button class="btn btn-primary" type="submit">Save</button>
          <button class="btn btn-outline-secondary" type="button" id="pf-cancel">Close</button>
        </div>
      </form>

      <div id="pf-upload" class="mt-4 d-none">
        <hr>
        <h5>Upload <span id="pf-upload-what"></span></h5>
        <p class="text-muted small" id="pf-upload-help"></p>
        <div style="gap:.5rem" class="d-flex gap-2 align-items-center">
          <input class="form-control" type="file" id="pf-file" style="max-width:28rem">
          <button class="btn btn-secondary" id="pf-upload-btn" type="button">Upload</button>
          <button class="btn btn-success" id="pf-editor-deploy" type="button">Deploy</button>
        </div>
        <div class="progress mt-2 d-none" id="pf-progress"><div class="progress-bar" style="width:0%"></div></div>
        <div class="small mt-2" id="pf-upload-result"></div>
      </div>
    </div>
  </div>

  <!-- Job -->
  <div class="card mb-4 d-none" id="pf-job">
    <div class="card-body">
      <h5 class="card-title"><span id="pf-job-title"></span> <span class="badge" id="pf-job-status"></span></h5>
      <ul class="list-unstyled mb-2" id="pf-job-steps"></ul>
      <details><summary class="small text-muted">Log</summary>
        <pre class="small mt-2 p-2 border rounded" id="pf-job-log" style="max-height:20rem;overflow:auto;white-space:pre-wrap"></pre>
      </details>
    </div>
  </div>

  <table class="table table-striped align-middle">
    <thead><tr>
      <th>Challenge</th><th>Category</th><th>Type</th><th>Points</th>
      <th>In CTFd</th><th>Instances / files</th><th class="text-end">Actions</th>
    </tr></thead>
    <tbody id="pf-rows"><tr><td colspan="7" class="text-muted">Loading…</td></tr></tbody>
  </table>
</div>
{% endblock %}

{% block scripts %}
<script>
{% raw %}
(function () {
  const root = document.getElementById("pf");
  const NONCE = root.dataset.nonce;
  const API = "/admin/platform/api/";
  const $ = (id) => document.getElementById(id);
  const form = $("pf-form");
  // F("id") / F("name") are the form's own attributes, not the inputs
  const F = (n) => form.elements.namedItem(n);
  let rows = [];
  let editing = null;      // challenge id being edited (null = new)
  let pollTimer = null;

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v);
    }
    for (const k of kids) if (k != null) e.append(k.nodeType ? k : document.createTextNode(String(k)));
    return e;
  }

  function alertBox(msg, kind) {
    const box = $("pf-alert");
    box.replaceChildren();
    if (!msg) return;
    box.append(el("div", {class: "alert alert-" + (kind || "danger")}, msg));
  }

  async function api(method, path, body, raw) {
    const opts = {method, credentials: "same-origin", headers: {"CSRF-Token": NONCE}};
    // CTFd accepts the CSRF-Token header only on JSON requests: send JSON
    // (an empty object if there's no body) for every non-GET call
    if (method !== "GET") { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body === undefined ? {} : body); }
    const r = await fetch(API + path, opts);
    let data = {};
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) {
      const d = data.detail;
      throw new Error(Array.isArray(d) ? d.join(" · ") : (d || ("HTTP " + r.status)));
    }
    return data;
  }

  // ── Table ────────────────────────────────────────────────────────────
  async function load() {
    try {
      const data = await api("GET", "challenges");
      rows = data.challenges;
      render();
      if (data.ctfd_error) alertBox("CTFd: " + data.ctfd_error, "warning");
      const running = (data.jobs || []).find(j => j.status === "running" || j.status === "queued");
      if (running && !pollTimer) watch(running.id);
    } catch (e) {
      alertBox("Couldn't load challenges: " + e.message);
    }
  }

  function badge(text, kind) { return el("span", {class: "badge bg-" + kind + " badge-" + kind}, text); }

  function render() {
    const tb = $("pf-rows");
    tb.replaceChildren();
    if (!rows.length) {
      tb.append(el("tr", {}, el("td", {colspan: 7, class: "text-muted"}, "No challenges yet. Click “New challenge”.")));
      return;
    }
    for (const r of rows) {
      const m = r.manifest;
      const svc = m.type !== "static";
      let ctfd = r.ctfd ? (r.ctfd.state === "visible" ? badge("Visible", "success") : badge("Hidden", "secondary"))
                        : badge("Not deployed", "warning");
      let inst;
      if (svc) {
        inst = el("span", {}, `${r.running} running / ${r.instances}`);
        if (r.pending_image) inst.append(" ", badge("image waiting for deploy", "info"));
      } else {
        inst = el("span", {}, r.zips ? `${r.zips} team zips` : "no zips yet");
        if (!r.has_files) inst.append(" ", badge("upload files", "warning"));
      }
      const acts = el("div", {class: "btn-group btn-group-sm"},
        el("button", {class: "btn btn-outline-secondary", onclick: () => openEditor(m.id)}, "Edit / upload"),
        el("button", {class: "btn btn-outline-success", onclick: () => deploy(m.id, r.instances)}, "Deploy"),
        r.ctfd ? el("button", {class: "btn btn-outline-secondary",
                              onclick: () => setVisible(m.id, r.ctfd.state !== "visible")},
                    r.ctfd.state === "visible" ? "Hide" : "Show") : null,
        el("button", {class: "btn btn-outline-danger", onclick: () => remove(m)}, "Delete"));
      tb.append(el("tr", {},
        el("td", {}, el("strong", {}, m.name), el("div", {class: "small text-muted"}, m.id)),
        el("td", {}, m.category),
        el("td", {}, svc ? (m.type === "http" ? "web" : "tcp") + " :" + m.port : "download"),
        el("td", {}, r.ctfd && r.ctfd.value != null ? r.ctfd.value : (m.initial || 500)),
        el("td", {}, ctfd),
        el("td", {}, inst),
        el("td", {class: "text-end"}, acts)));
    }
  }

  // ── Editor ───────────────────────────────────────────────────────────
  function slug(s) {
    return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase()
            .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "");
  }

  function syncTypeFields() {
    const svc = F("type").value !== "static";
    document.querySelectorAll(".pf-service").forEach(e => e.classList.toggle("d-none", !svc));
    F("port").required = svc;
  }

  function openEditor(id) {
    editing = id;
    form.reset();
    const r = rows.find(x => x.manifest.id === id);
    $("pf-editor-title").textContent = id ? "Edit " + id : "New challenge";
    F("id").readOnly = !!id;
    F("type").disabled = !!id;
    if (r) {
      const m = r.manifest;
      for (const k of ["name", "id", "category", "type", "port", "memory_limit", "image", "initial", "minimum", "decay", "description", "author"]) {
        if (m[k] != null && F(k)) F(k).value = m[k];
      }
    }
    syncTypeFields();
    showUpload(id ? r.manifest : null);
    $("pf-editor").classList.remove("d-none");
    $("pf-editor").scrollIntoView({behavior: "smooth"});
  }

  function showUpload(m) {
    const box = $("pf-upload");
    $("pf-upload-result").textContent = "";
    $("pf-progress").classList.add("d-none");
    if (!m) { box.classList.add("d-none"); return; }
    box.classList.remove("d-none");
    const file = $("pf-file");
    file.value = "";
    if (m.type === "static") {
      $("pf-upload-what").textContent = "files (.zip)";
      $("pf-upload-help").textContent = "A zip of the files players download. Put {{FLAG}} where each team's flag goes; every team gets its own copy. Max 50 MB.";
      file.accept = ".zip";
    } else {
      $("pf-upload-what").textContent = "image (.tar)";
      $("pf-upload-help").textContent = "Build the image on your machine, then: docker save -o " + m.id + ".tar <image>. The container must read its flag from the FLAG environment variable and listen on port " + m.port + ".";
      file.accept = ".tar";
    }
  }

  F("type").addEventListener("change", syncTypeFields);
  F("name").addEventListener("input", () => { if (!editing) F("id").value = slug(F("name").value); });
  $("pf-new").onclick = () => openEditor(null);
  $("pf-cancel").onclick = () => { $("pf-editor").classList.add("d-none"); editing = null; };
  $("pf-refresh").onclick = load;

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = {};
    for (const k of ["name", "category", "type", "description", "author"]) body[k] = F(k).value.trim();
    for (const k of ["initial", "minimum", "decay"]) if (F(k).value !== "") body[k] = parseInt(F(k).value, 10);
    if (body.type !== "static") {
      body.port = parseInt(F("port").value, 10);
      body.memory_limit = F("memory_limit").value;
    }
    const id = editing || F("id").value.trim();
    try {
      const res = await api("PUT", "challenges/" + encodeURIComponent(id), body);
      editing = id;
      F("id").readOnly = true;
      F("type").disabled = true;
      F("image").value = res.manifest.image || "";
      alertBox(res.created ? "Saved. Now upload the " + (body.type === "static" ? "files" : "image") + ", then Deploy." : "Saved.", "success");
      showUpload(res.manifest);
      await load();
    } catch (e) {
      alertBox("Not saved: " + e.message);
    }
  });

  $("pf-upload-btn").onclick = () => {
    const f = $("pf-file").files[0];
    if (!editing || !f) { alertBox("Choose a file first."); return; }
    const bar = $("pf-progress"), fill = bar.firstElementChild;
    bar.classList.remove("d-none"); fill.style.width = "0%";
    $("pf-upload-result").textContent = "Uploading " + f.name + "…";
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API + "challenges/" + encodeURIComponent(editing) + "/upload");
    xhr.setRequestHeader("CSRF-Token", NONCE);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) fill.style.width = (100 * e.loaded / e.total).toFixed(0) + "%"; };
    xhr.onload = () => {
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status >= 200 && xhr.status < 300) {
        $("pf-upload-result").textContent = "✔ Uploaded" + (d.mb ? " (" + d.mb + " MB)" : "") + (d.files ? " (" + d.files + " files)" : "") + ". " + (d.next || "");
        load();
      } else {
        $("pf-upload-result").textContent = "✘ " + (d.detail || ("HTTP " + xhr.status));
      }
    };
    xhr.onerror = () => { $("pf-upload-result").textContent = "✘ upload failed (network)"; };
    xhr.send(f);
  };

  $("pf-editor-deploy").onclick = () => {
    const r = rows.find(x => x.manifest.id === editing);
    deploy(editing, r ? r.instances : 0);
  };

  // ── Actions ──────────────────────────────────────────────────────────
  async function deploy(id, instances) {
    let replace = 0;
    if (instances > 0) {
      if (!confirm("Deploy " + id + "?")) return;
      replace = confirm("Also recreate its " + instances + " team instances?\n\nNeeded after a new image, port or memory. Running instances stop; teams just press Start again.\n\nOK = recreate · Cancel = keep them") ? 1 : 0;
    }
    try {
      const res = await api("POST", "challenges/" + encodeURIComponent(id) + "/deploy" + (replace ? "?replace=1" : ""));
      watch(res.job);
    } catch (e) { alertBox("Deploy failed to start: " + e.message); }
  }

  $("pf-deploy-all").onclick = async () => {
    if (!confirm("Deploy every challenge? Missing instances and flags are created; existing ones are kept.")) return;
    try { watch((await api("POST", "deploy-all")).job); } catch (e) { alertBox(e.message); }
  };

  async function setVisible(id, visible) {
    try {
      await api("POST", "challenges/" + encodeURIComponent(id) + "/visibility", {state: visible ? "visible" : "hidden"});
      await load();
    } catch (e) { alertBox(e.message); }
  }

  async function remove(m) {
    const typed = prompt("Delete " + m.name + " everywhere: team instances, the CTFd challenge with its solves, and uploaded files.\n\nType the id to confirm: " + m.id);
    if (typed !== m.id) return;
    try { watch((await api("DELETE", "challenges/" + encodeURIComponent(m.id))).job); } catch (e) { alertBox(e.message); }
  }

  // ── Job progress ─────────────────────────────────────────────────────
  const ICON = {running: "⏳", done: "✔", failed: "✘"};

  // While a job runs: disable the buttons that start jobs, keep the
  // progress card in view
  function setBusy(busy) {
    document.querySelectorAll("#pf-rows button, #pf-deploy-all, #pf-editor-deploy")
      .forEach(b => { if (!/Edit/.test(b.textContent)) b.disabled = busy; });
  }

  function watch(jobId) {
    clearInterval(pollTimer);
    $("pf-job").classList.remove("d-none");
    $("pf-job").scrollIntoView({behavior: "smooth", block: "center"});
    setBusy(true);
    const tick = async () => {
      let j;
      try { j = await api("GET", "jobs/" + jobId); } catch (e) { clearInterval(pollTimer); pollTimer = null; setBusy(false); alertBox(e.message); return; }
      $("pf-job-title").textContent = j.title;
      const st = $("pf-job-status");
      st.textContent = j.status;
      const kind = {done: "success", failed: "danger", running: "primary"}[j.status] || "secondary";
      st.className = "badge bg-" + kind + " badge-" + kind;
      const ul = $("pf-job-steps");
      ul.replaceChildren(...j.steps.map(s => el("li", {}, (ICON[s.status] || "·") + " " + s.name + (s.detail ? " — " + s.detail : ""))));
      if (j.error) ul.append(el("li", {class: "text-danger"}, "✘ " + j.error));
      const log = $("pf-job-log");
      log.textContent = (j.log || []).join("\n");
      log.scrollTop = log.scrollHeight;
      if (j.status === "done" || j.status === "failed") {
        clearInterval(pollTimer); pollTimer = null;
        setBusy(false);
        load();
      } else {
        setBusy(true);  // the table may have been redrawn meanwhile
      }
    };
    tick();
    pollTimer = setInterval(tick, 1500);
  }

  if (root.dataset.configured !== "1") {
    alertBox("The dashboard isn't connected: CONTROLLER_ADMIN_KEY is missing in CTFd. Run make secrets && make deploy.");
  }
  load();
})();
{% endraw %}
</script>
{% endblock %}
"""
