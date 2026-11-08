# Changelog

## v1.1.0 (2026-11-08)

Admin dashboard: add challenges from the browser. Tested on the dry-run VM
with the D1–D17 checklist in [docs/DASHBOARD.md](docs/DASHBOARD.md).

### Added
- **Admin → Platform** page in CTFd: create a challenge with a form, upload a
  `docker save` image or a zip of `{{FLAG}}` files, Deploy with live
  progress, Show/Hide, Delete. See [docs/DASHBOARD.md](docs/DASHBOARD.md)
- `controller-admin` service (dashboard backend, single job queue) replacing
  the scaled-to-zero provisioner; pushes images with `crane`
- `platform-data` volume for uploads and per-team download zips

### Changed
- `make challenges` runs the same deploy pipeline as the dashboard and no
  longer replaces challenges added from the browser
- Download zips are served from the volume instead of being built into the
  static-files image
- `make new-round` also clears the challenge list and uploaded files;
  `make backup` / `make restore` include the challenge list
- New secrets: `ADMIN_KEY`, `controller-admin-key`, `registry-creds`,
  `ctfd-admin-token` (`make secrets`, `make ctfd-setup`)
- "Deploy all" skips download challenges that have no files yet instead of
  failing; `make challenges` rebuilds repo zips after a new round or restore
- `make build-controller` also restarts controller-admin

### Fixed
- bulk_spawn reported existing instances as "created"

### Upgrading from v1.0.0
```bash
git fetch && git checkout v1.1.0
make secrets && make build && make deploy && make ctfd-setup && make challenges
```
Set `CTFD_VERSION=3.8.9` in config.env if it still says `latest`.

## v1.0.0 (2026-11-07)

First release. The InfoDays cloud CTF platform, rebuilt to run on one or two
Ubuntu laptops or VMs with k3s.

### Platform
- k3s single-node install with an optional challenge node (`make join-script`)
- CTFd 3.8.9 with the controller plugin built in, 2 replicas behind Traefik
  on ports 80/443, HTTPS with a local CA
- Password-protected image registry on the core laptop
- Per-team challenge instances (start/stop from CTFd, 60-minute auto-stop,
  up to 4 running per team)
- Per-team HMAC flags with a cheating log, per-team file downloads
- Network policies: challenges can't reach CTFd, the registry, other teams
  or the internet
- `make` commands for install, rounds, backup/restore, status and IP changes

### Tested
Full dry run in VMware Fusion VMs (Ubuntu 24.04): install, play from a
phone, flag sharing, isolation on both nodes, auto-stop, capacity,
new round, backup/restore, reboot, IP change, second-node failover.
See [docs/DRY-RUN.md](docs/DRY-RUN.md#results-log).

### Fixed during the dry run
- 502 errors during CTFd restarts
- Players logged out after opening a web challenge (cookie name clash;
  CTFd now uses a Secure `ctfd_session` cookie)
- ~8 s delay on every Start and failed bulk_spawn, caused by the DHCP search
  domain (fixed with `ndots:2`)
- Whole platform down after a systemd-networkd restart (it deleted the pod
  and Docker routes)
- Home page 404, late Start button, `kubectl` needing sudo
- InfoDays theme now optional (`BRANDING=false` by default)

### Known limitations
- Challenges share CTFd's IP (different ports), so challenge authors must not
  use the cookie names `session` or `ctfd_session`
- Instances on a failed challenge laptop take about 5 minutes to move
- No web UI for adding challenges yet (planned: admin dashboard)
