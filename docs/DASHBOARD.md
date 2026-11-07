# Admin dashboard (Admin → Platform)

Add a challenge from the browser: fill in a form, upload its image or files,
click **Deploy**. The platform pushes the image, creates the CTFd challenge,
a stopped instance for every team, each team's flag, and per-team download
zips. No SSH, YAML or `make` needed.

In CTFd: log in as admin → **Admin Panel** → **Plugins** menu → **Platform**
(or open `https://CTF_HOST/admin/platform`).

## Preparing a challenge

**Web or TCP service** (players get their own instance):

1. Build the image on any machine with Docker. The container must:
   - read its flag from the **`FLAG`** environment variable (every team's differs)
   - listen on one port (8080 for web is usual)
   - not set a cookie called `session` or `ctfd_session`
   `challenges/templates/` has working examples.
2. Save it to a file:
   ```bash
   docker build -t my-chal .
   docker save -o my-chal.tar my-chal
   ```
   Max 1 GB. Smaller images deploy faster: prefer `-alpine` / `-slim` bases.

**Download challenge** (per-team files):

Zip the files players receive, writing `{{FLAG}}` where each team's flag
goes (text files). Max 50 MB, 500 files. Binary files that need the flag
compiled in still use `build.sh` with `make challenges` (see the README).

## Adding it

1. **+ New challenge**. Name, category, type, port, memory, points,
   description. The ID is generated from the name; it's part of every flag,
   so it can't change later.
2. **Save**. The upload box appears.
3. **Upload** the `.tar` or `.zip`. It's checked straight away (a `docker
   save` archive with one image / a safe zip containing `{{FLAG}}`).
4. **Deploy**. Watch the steps:
   *Check manifests → Push images → Add to CTFd → Reload controller → Create
   team instances → Register team flags → Build download files*.
5. New challenges start **Hidden**. Try it yourself (see below), then **Show**.

**Testing before players see it:** admins have no team, so the challenge
page starts instances as **team 1**. Start it, solve it, then **Stop** it so
team 1's instance isn't left running.

## Changing or removing

| Action | Effect |
|---|---|
| **Edit / upload** → Save | Name, description, category: updated in CTFd on the next Deploy |
| Upload a new image → **Deploy** → *recreate instances: OK* | New image for every team. Running instances stop; teams press Start again |
| Change port or memory → **Deploy** → *recreate: OK* | Same: needed so instances get the new settings |
| **Hide / Show** | CTFd visibility, immediately |
| **Delete** (type the ID) | Removes team instances, the CTFd challenge **with its solves**, and uploaded files |
| **Deploy all** | Creates whatever is missing (e.g. instances and zips for a team added mid-round) |

Points for an existing CTFd challenge are set in CTFd itself
(Admin → Challenges), not by re-deploying.

## Dashboard and `make challenges` together

Both write to the same challenge list (the `challenge-manifests`
ConfigMap) and run the same deploy code:
- `make challenges` adds/updates the challenges in `challenges/manifests/`
  and leaves dashboard ones alone.
- `make new-round` clears the list, the download files and the uploads
  (images stay in the registry).
- `make backup` saves the challenge list with the database; `make restore`
  puts it back. Uploaded images and zips are **not** in backups: keep the
  originals, or click **Deploy all** after a restore to rebuild zips.

## Upgrading an installation from v1.0.0

```bash
git fetch && git checkout <release>
make secrets          # adds ADMIN_KEY and the dashboard's secrets
make build            # controller (now with crane), CTFd plugin, static-files
make deploy           # controller-admin + storage volume
make ctfd-setup       # hands the CTFd admin token to the dashboard
make challenges       # download zips now live on the volume: rebuild them
```

## How it works

```
browser ──https──► CTFd plugin /admin/platform/api/*   (CTFd admin + CSRF check)
                        │  X-Admin-Key
                        ▼
                  controller-admin :5001  (1 process, job queue)
                        ├─ crane push ─────► registry :5000
                        ├─ ConfigMap challenge-manifests
                        ├─ CTFd API (challenges, flags)
                        ├─ Kubernetes (team deployments, services, flag secrets)
                        └─ /data volume ◄─── static-files (read-only) serves zips
```

- controller-admin is reachable only from CTFd pods (network policy); the
  proxy forwards a fixed list of paths and only for CTFd admins.
- Image uploads are never built or run in the platform: `crane` copies the
  saved layers to the registry. Challenge containers keep their isolation
  (no outbound network, memory limits, no service-account token).
- Uploaded zips are checked for `..`, absolute paths, symlinks, `build.sh`,
  size and file count before anything is extracted.

---

## VM test checklist (v1.1 dashboard)

On the dry-run VM, after the upgrade steps above. On your Mac, make the test
files:

```bash
cd local-ctf/challenges/templates/web && docker build -t dash-web . && docker save -o ~/dash-web.tar dash-web
mkdir -p /tmp/dz && echo 'Your flag: {{FLAG}}' > /tmp/dz/readme.txt && (cd /tmp/dz && zip -q ~/dash-files.zip readme.txt)
```
(No Docker on the Mac? Do the first line in the VM: `docker` works there, then
`scp` the tar to the Mac.)

| # | Do | Expect |
|---|---|---|
| D1 | `kubectl -n ctfd get pods` | `controller-admin` Running, no `controller-provisioner` |
| D2 | Admin → Plugins → **Platform** | page loads, lists the existing example challenges with instance counts |
| D3 | **New challenge**: "Dash Web", Web, web service, port 8080 → Save | "Saved. Now upload…", upload box appears |
| D4 | Upload `dash-web.tar` | progress bar, "✔ Uploaded (… MB)" |
| D5 | Upload a random file (e.g. a .txt renamed .tar) | "✘ not a tar file…", nothing saved |
| D6 | **Deploy** | all steps ✔; "3 created" instances; challenge shows *Hidden* |
| D7 | Show it; as **alice**: open it → Start → solve → submit | instance URL works, flag **Correct** |
| D8 | As **bob**: submit alice's flag | **Incorrect**, appears in Admin → Cheaters |
| D9 | **New challenge** "Dash Files", Download → Save → upload `dash-files.zip` → Deploy → Show | step *Build download files* ✔ "3 team zips"; alice downloads, readme has her flag, submitting it is Correct |
| D10 | Upload a zip whose file has no `{{FLAG}}` | rejected with a clear message |
| D11 | Edit "Dash Web", memory 256Mi → Save → Deploy → recreate: OK | instances recreated (`kubectl -n challenges get deploy -l challenge=dash-web -o yaml \| grep memory`) |
| D12 | **Delete** "Dash Files" (type its id) | job ✔; gone from CTFd and the table |
| D13 | Example **Welcome** download still works for alice | zip with her flag (served from the volume now) |
| D14 | `make challenges` | still works; dashboard challenges untouched |
| D15 | `make backup NAME=dash` then `make new-round` | Platform page empty after the reset |
| D16 | `make restore BACKUP=backups/*_dash` | challenge list back on the Platform page |
| D17 | Log in as **alice**, open `https://CTF_HOST/admin/platform/api/challenges` | redirected/denied, never the JSON |
