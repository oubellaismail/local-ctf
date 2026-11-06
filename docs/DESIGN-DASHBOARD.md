# Design: admin dashboard + one-click VM install

Status: **proposal**, nothing built yet. Prerequisite: the dry run in
[DRY-RUN.md](DRY-RUN.md) passes on a VM. Don't build on an unverified base.

## Goal

A club organiser who doesn't know Kubernetes can run a round:

1. Import the platform VM, start it, and open the URL it shows.
2. In CTFd's admin panel, add a challenge (form + upload) and click Deploy.
3. Check that it works, then publish it to the teams.
4. Start a new round, back up, and restore, all from the browser.

The CLI (`make ...`) stays and remains the source of truth: the dashboard
calls the same code, so SSH is always a fallback.

## Non-goals (v1)

- No in-browser code editor or Dockerfile editor.
- No multi-event support (one CTFd = one event).
- No user-facing changes (players see the same CTFd as today).
- No public-internet hosting, so the campus-LAN-only security model stays.

---

## Architecture

```
 admin browser
      │  https://CTF_HOST/admin/platform/...         (CTFd admin session)
      ▼
┌─ CTFd + controller-plugin ─────────────┐
│  new admin pages (Jinja + plugin.js)   │
│  /admin/platform/api/* → proxies to ───┼──► controller-api  (new /admin/* routes,
└────────────────────────────────────────┘     X-Controller-Key + X-Admin-Key)
                                                   │
                     ┌─────────────────────────────┼───────────────────────────┐
                     ▼                             ▼                           ▼
            Kubernetes API (challenges ns)   build Jobs (kaniko,        ConfigMap challenge-manifests
            scale / list / delete            ns ctf-build)              + CTFd API (sync, flags)
                                                   │
                                                   ▼
                                            registry :5000
```

Why inside CTFd and not a separate app: admins already have accounts, CTFd's
`@admins_only` handles authorization, and the plugin already proxies to the
controller (`/controller-proxy/*`). A separate app would add its own logins,
permissions and server to maintain.

### Moving script logic into the controller

The work is mostly refactoring, because the logic already exists:

| Today (script) | Becomes (controller module) |
|---|---|
| `scripts/ctfd_sync.py validate/sync` | `controller/manifests.py`: `validate()`, `sync_to_ctfd()` |
| `controller/bulk_spawn.py` | callable `spawn_all(manifest_ids=None)` (already a module) |
| `controller/import_flags.py` | callable `import_flags(manifest_ids=None)` |
| `scripts/build-static.py` | `controller/static_build.py` (writes to a PVC instead of baking an image) |
| `scripts/backup.sh` (mysqldump) | `controller/backup.py`: dump to the `backups` PVC |
| `scripts/new-round.sh` | `controller/rounds.py` |

The scripts then become thin wrappers (`kubectl exec ... python -m ...`), so
the CLI and dashboard can never drift apart.

**Manifest storage changes**: today manifests are files in
`challenges/manifests/`, copied into a ConfigMap. With uploads they must live
in the cluster. v1: the ConfigMap is the source of truth, and the controller
writes it. `make export-manifests` dumps it back to YAML files for git.

**Static files change**: instead of rebuilding the `static-files` image, the
controller writes per-team zips to a PVC (`static-data`) that `static-files`
mounts read-only. No image rebuild per challenge.

---

## Pages

All under the CTFd admin navbar → **Platform**.

### 1. Challenges: `/admin/platform/challenges`

Table: id, name, category, type, image status (built / building / failed),
instances (created / running), visible in CTFd (yes/no). Actions per row:
Edit, Test, Redeploy, Delete.

**New challenge** form (fields = manifest schema):

- id (auto-suggested from name, validated `^[a-z0-9-]{1,40}$`), name, category,
  description (markdown preview), author
- type: `http` / `tcp` / `static`
- port, extra ports, memory limit (128Mi–512Mi dropdown), run as root
  (checkbox with a warning)
- scoring: initial / minimum / decay
- **Source**, one of:
  - **Image tar** (`docker save -o chal.tar img`): pushed as-is with
    `crane push`, no build. Fastest and safest.
  - **Build context zip** (Dockerfile at the root, max 50 MB): built by a
    kaniko Job.
  - **Static files** (`type: static`): zip of files containing `{{FLAG}}`,
    or containing `build.sh`.
- Visibility after deploy: hidden (default) / visible

**Deploy** runs these steps and shows live progress for each:
`validate → upload → build/push → save manifest → sync to CTFd → spawn
instances → import flags → done`. Each step's log is viewable, and a failure
stops at that step with the log.

**Test**: starts the instance for a hidden admin team (`__admin_test`, created
at setup), shows the URL, and shows that team's flag so the admin can check
the solve. Stopped automatically after 15 min.

### 2. Instances: `/admin/platform/instances`

- Grid of teams × service challenges: stopped / starting / running / error,
  with remaining lifetime. Click a cell to Stop, Restart, view Logs (last 200
  lines), or Extend +30 min.
- Filters: running only, by team, by challenge.
- **Stop all instances** (confirm dialog).

### 3. Platform: `/admin/platform`

- Nodes: name, role, Ready, CPU %, RAM %, pods. Red if RAM > 80 %.
- Capacity: running instances / quota (`challenge-limits` ResourceQuota).
- Service health: ctfd, mysql, redis, controller, registry, static-files.
- Disk: registry volume, backups PVC, free space on the node.
- **Rounds:** New round (keep teams / full reset, confirm by typing
  `reset`), Backup now, list of backups with Download and Restore buttons.
  Restore needs the typed confirmation too.

---

## Controller API additions

All under `/admin/*`, requiring **both** `X-Controller-Key` (as today) and a
new `X-Admin-Key` that only the plugin's admin routes send. The plugin checks
`is_admin()` before forwarding, so the player-facing proxy routes can never
reach `/admin/*`.

| Method + path | Does |
|---|---|
| `GET /admin/challenges` | manifests + build status + instance counts |
| `POST /admin/challenges` | create from form JSON (manifest), returns `job_id` |
| `PUT /admin/challenges/{id}` | update manifest (id immutable) |
| `DELETE /admin/challenges/{id}` | delete deployments/services/secrets, CTFd challenge, manifest |
| `POST /admin/challenges/{id}/upload` | multipart: image tar / context zip / static zip, streamed to the `uploads` PVC |
| `POST /admin/challenges/{id}/deploy` | runs the pipeline as a background job, returns `job_id` |
| `GET /admin/jobs/{job_id}` | step list, current step, per-step log tail |
| `POST /admin/challenges/{id}/test` | starts the instance for `__admin_test` |
| `GET /admin/instances` | all team × challenge states |
| `POST /admin/instances/{team}/{chal}/{action}` | stop / restart / extend |
| `GET /admin/instances/{team}/{chal}/logs` | pod logs tail |
| `POST /admin/instances/stop-all` | scale all to 0 |
| `GET /admin/platform` | nodes, metrics, quota, health, disk |
| `POST /admin/backups` / `GET /admin/backups` / `POST /admin/backups/{name}/restore` | |
| `POST /admin/rounds/new` | body `{mode: keep-teams|full}` |

Jobs run in a background worker thread inside the controller (one at a
time, queued), and their state lives in Redis so a controller restart shows
"interrupted" instead of losing them. A single-worker queue is enough for 20
teams.

**RBAC**: the controller-api ServiceAccount gains the provisioner's rights
(create/delete Deployments, Services, Secrets in `challenges`), plus create
Jobs in `ctf-build` and patch the `challenge-manifests` ConfigMap. The
`controller-provisioner` deployment goes away.

---

## Image builds (security-sensitive)

Uploaded Dockerfiles run arbitrary commands at build time. Rules:

- Never use Docker on the host, and never mount `/var/run/docker.sock` or
  containerd's socket into any pod. That is root on the laptop.
- Each build is a Kubernetes **Job** in a dedicated namespace `ctf-build`
  running `gcr.io/kaniko-project/executor` (pinned digest, mirrored into our
  registry by `make build`):
  - build context: the uploaded zip, extracted by an init container from the
    `uploads` PVC
  - no ServiceAccount token, no host mounts, runs as non-root where kaniko
    allows (kaniko needs root in-container but no privileges)
  - limits: 2 CPU, 2 GiB RAM, 15 min `activeDeadlineSeconds`, 1 concurrent
    build
  - NetworkPolicy: egress to the registry + internet package mirrors only
    (apt/pypi/npm need it), no access to the `ctfd` namespace
  - pushes with a registry credential scoped to `challenges/*` (htpasswd user
    `builder`, separate from the platform user)
- Image tar uploads skip all of this: `crane push` from the controller.
  Recommend it in the UI.
- Upload limits: 500 MB tar, 50 MB zip. Zips are checked for path traversal
  (`..`, absolute paths, symlinks) before extraction.
- Only CTFd admins can reach any of it. Make the number of CTFd admins small.

---

## One-click VM install

### The VM image (`.ova`)

Built with **Packer** (`packer/ubuntu-ctf.pkr.hcl`) from the Ubuntu Server
24.04 amd64 ISO via autoinstall:

- 4 vCPU, 10 GB RAM, 60 GB thin disk, one NIC
- k3s, Docker, registry, all platform images **pre-pulled and pre-built**
  (setup works without internet)
- The repo is at `/opt/local-ctf`, and the `ctf` user runs it
- No secrets baked in: everything is generated on first boot
- Exported as OVA, which imports into VMware Fusion/Workstation, VirtualBox,
  and KVM (via `virt-v2v` or converting the VMDK).

### First boot: `ctf-firstboot.service`

1. Wait for an IPv4 on the NIC and set `CTF_HOST` to that IP.
2. `make secrets` (new master secret, DB passwords, CA + cert for that IP).
3. Set the k3s node IP and start k3s, then `make deploy`, then `make ctfd-setup` (admin only, no teams yet).
4. Write the URL + admin password to `/etc/issue`, so they show on the VM
   console's login screen:
   ```
   CTF platform ready:  https://10.20.5.17
   Admin: admin / <password>   (change it after first login)
   CA certificate:      http://10.20.5.17/ca.crt
   ```
5. Disable itself.

### IP change: `ctf-reip.service` (every boot)

If the IP differs from `CTF_HOST`: update config, regenerate the server
cert (same CA, so players who installed the CA keep trusting it), update
the k3s `--node-ip`, restart k3s, redeploy ingress, and refresh `/etc/issue`.
This covers moving the VM to another laptop and switching between NAT and
bridged. Also exposed as `make reip`.

### Also usable on bare metal

The same autoinstall `user-data` builds a USB installer ISO for laptops that
run Ubuntu Server natively (no VM). Same first-boot service.

---

## Milestones

| # | Deliverable | Done when |
|---|---|---|
| 0 | Dry run passes in a Fusion VM | DRY-RUN pass criteria all ✔ |
| 1 | `make reip`, firstboot + reip services, Packer OVA | fresh OVA import → URL on console → CTFd works, no terminal used |
| 2 | Refactor scripts into controller modules (CLI unchanged) | dry run phases 4–7 still pass, unit tests for each module |
| 3 | Admin API + Challenges page with **image tar** upload + Test | an organiser adds a web challenge from a `.tar` and solves it as a team |
| 4 | kaniko build Jobs + context zip upload, static zip upload | same, from a Dockerfile zip; build pod can't reach CTFd |
| 5 | Instances page | stop/restart/logs/extend work, stop-all works |
| 6 | Platform page: health, backups, rounds | full round lifecycle from the browser |

Each milestone ends with a commit and a short re-run of the dry-run steps it
touches.

## Open questions

- **Admin accounts**: one shared admin or one per organiser? Per-organiser
  is better (CTFd logs who did what) and costs nothing.
- **Internet in the VM during builds**: Dockerfiles that `apt install` need
  it. If the campus network blocks or proxies it, recommend image tars.
- **Where backups go off-machine**: the Download button covers it manually.
  Automatic copy to a USB drive or another laptop is out of scope for v1.
